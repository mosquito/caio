// caio Go baseline benchmark
// Same sweeps as bench.py — outputs bench_go.csv in the same format.
//
// Usage:
//   go run bench_go.go -data /tmp/caio-bench -results /tmp/results
package main

import (
	"encoding/csv"
	"flag"
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"sync"
	"time"
)

var (
	dataDir    = flag.String("data", "/tmp/caio-bench", "directory with *.bin files")
	resultsDir = flag.String("results", "/tmp/results", "output directory")
	totalOps   = flag.Int("ops", 10000, "ops per cell")
	warmupOps  = flag.Int("warmup", 500, "warmup ops")
)

const (
	concSweepChunk  = 16 * 1024
	chunkSweepConc  = 64
	backend         = "go_goroutine"
)

var concSweep  = []int{1, 4, 16, 64, 256, 512, 1024, 2048, 4096}
var chunkSweep = []int{4 * 1024, 16 * 1024, 64 * 1024, 256 * 1024, 1024 * 1024, 4 * 1024 * 1024}

// ── file pool ────────────────────────────────────────────────────────────────

type fileEntry struct {
	f    *os.File
	size int64
}

type readPool struct {
	files []fileEntry
}

func openPool(dir string) (*readPool, error) {
	matches, err := filepath.Glob(filepath.Join(dir, "*.bin"))
	if err != nil || len(matches) == 0 {
		return nil, fmt.Errorf("no .bin files in %s", dir)
	}
	p := &readPool{}
	for _, path := range matches {
		f, err := os.Open(path)
		if err != nil {
			return nil, err
		}
		st, _ := f.Stat()
		p.files = append(p.files, fileEntry{f, st.Size()})
	}
	return p, nil
}

func (p *readPool) randArgs(chunk int) (f *os.File, offset int64) {
	e := p.files[rand.Intn(len(p.files))]
	hi := e.size - int64(chunk)
	if hi < 0 {
		hi = 0
	}
	off := rand.Int63n(hi+1) &^ 0xFFF // 4 KB aligned
	return e.f, off
}

func (p *readPool) seqArgs(chunk int) []struct {
	f      *os.File
	offset int64
} {
	var out []struct {
		f      *os.File
		offset int64
	}
	for _, e := range p.files {
		for off := int64(0); off+int64(chunk) <= e.size; off += int64(chunk) {
			out = append(out, struct {
				f      *os.File
				offset int64
			}{e.f, off})
		}
	}
	return out
}

func (p *readPool) close() {
	for _, e := range p.files {
		e.f.Close()
	}
}

// ── engine ───────────────────────────────────────────────────────────────────

// runRead runs n pread ops at `concurrency` goroutines in parallel.
// Returns (per-op latencies in µs, wall-clock seconds for the whole batch).
func runRead(pool *readPool, concurrency, n, chunk int, sequential bool) ([]float64, float64) {
	sem := make(chan struct{}, concurrency)
	lats := make([]float64, n)

	var seqSlice []struct {
		f      *os.File
		offset int64
	}
	var seqIdx int
	var seqMu sync.Mutex
	if sequential {
		seqSlice = pool.seqArgs(chunk)
	}

	var wg sync.WaitGroup
	wg.Add(n)

	t0 := time.Now()
	for i := 0; i < n; i++ {
		sem <- struct{}{}
		idx := i
		go func() {
			defer func() {
				<-sem
				wg.Done()
			}()

			var f *os.File
			var offset int64
			if sequential {
				seqMu.Lock()
				entry := seqSlice[seqIdx%len(seqSlice)]
				seqIdx++
				seqMu.Unlock()
				f, offset = entry.f, entry.offset
			} else {
				f, offset = pool.randArgs(chunk)
			}

			b := make([]byte, chunk)
			opT0 := time.Now()
			_, _ = f.ReadAt(b, offset)
			lats[idx] = float64(time.Since(opT0).Nanoseconds()) / 1000.0
		}()
	}
	wg.Wait()
	wall := time.Since(t0).Seconds()
	return lats, wall
}

func runWrite(concurrency, n, chunk int, sequential bool) ([]float64, float64, func()) {
	f, _ := os.CreateTemp("", "caio-bench-write-*")
	size := int64(1024 * 1024 * 1024)
	f.Seek(size-1, 0)
	f.Write([]byte{0})

	buf := make([]byte, chunk)
	rand.Read(buf)

	sem := make(chan struct{}, concurrency)
	lats := make([]float64, n)
	var wg sync.WaitGroup
	wg.Add(n)

	seqIdx := 0
	var seqMu sync.Mutex

	t0 := time.Now()
	for i := 0; i < n; i++ {
		sem <- struct{}{}
		idx := i
		go func() {
			defer func() {
				<-sem
				wg.Done()
			}()

			var offset int64
			if sequential {
				seqMu.Lock()
				offset = int64(seqIdx%int(size/int64(chunk))) * int64(chunk)
				seqIdx++
				seqMu.Unlock()
			} else {
				hi := size - int64(chunk)
				offset = rand.Int63n(hi+1) &^ 0xFFF
			}

			opT0 := time.Now()
			f.WriteAt(buf, offset)
			lats[idx] = float64(time.Since(opT0).Nanoseconds()) / 1000.0
		}()
	}
	wg.Wait()
	wall := time.Since(t0).Seconds()

	cleanup := func() {
		name := f.Name()
		f.Close()
		os.Remove(name)
	}
	return lats, wall, cleanup
}

// ── stats ────────────────────────────────────────────────────────────────────

func mean(xs []float64) float64 {
	s := 0.0
	for _, x := range xs {
		s += x
	}
	return s / float64(len(xs))
}

func pct(xs []float64, p float64) float64 {
	cp := make([]float64, len(xs))
	copy(cp, xs)
	// simple insertion sort is fine for small slices; use sort for large
	for i := 1; i < len(cp); i++ {
		for j := i; j > 0 && cp[j] < cp[j-1]; j-- {
			cp[j], cp[j-1] = cp[j-1], cp[j]
		}
	}
	idx := int(float64(len(cp)) * p)
	if idx >= len(cp) {
		idx = len(cp) - 1
	}
	return cp[idx]
}

// ── main ─────────────────────────────────────────────────────────────────────

func main() {
	flag.Parse()

	pool, err := openPool(*dataDir)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer pool.close()

	os.MkdirAll(*resultsDir, 0755)
	outPath := filepath.Join(*resultsDir, "bench_go.csv")
	outF, _ := os.Create(outPath)
	w := csv.NewWriter(outF)
	w.Write([]string{"backend", "sweep", "op", "concurrency", "chunk_bytes", "latency_us"})

	hdr := fmt.Sprintf("%-14s %-5s %6s  %8s  %6s  %7s  %7s  %7s  %7s  %8s  (ms)",
		"backend", "op", "conc", "ops/s", "MB/s", "mean", "p50", "p95", "p99", "p999")

	sep := "────────────────────────────────────────────────────────────────────────────────────────────────────────"

	// ── concurrency sweeps (fixed chunk, vary concurrency) ───────────────────
	type concSweepSpec struct {
		tag   string
		label string
		seq   bool
	}
	concSweeps := []concSweepSpec{
		{"conc_sweep_rand", "SWEEP 1 — concurrency / random", false},
		{"conc_sweep_seq",  "SWEEP 3 — concurrency / sequential", true},
	}
	for _, sw := range concSweeps {
		fmt.Printf("\n%s\n%s\n%s\n", sw.label, hdr, sep)
		for _, conc := range concSweep {
			runRead(pool, conc, *warmupOps, concSweepChunk, sw.seq)
			lats, wallR := runRead(pool, conc, *totalOps, concSweepChunk, sw.seq)
			opsS := float64(*totalOps) / wallR
			mbS := opsS * float64(concSweepChunk) / 1e6
			fmt.Printf("%-14s %-5s %6d  %8.0f  %6.1f  %7.3f  %7.3f  %7.3f  %7.3f  %8.3f\n",
				backend, "read", conc, opsS, mbS,
				mean(lats)/1000, pct(lats, 0.5)/1000, pct(lats, 0.95)/1000,
				pct(lats, 0.99)/1000, pct(lats, 0.999)/1000)
			for _, l := range lats {
				w.Write([]string{backend, sw.tag, "read", fmt.Sprint(conc), fmt.Sprint(concSweepChunk), fmt.Sprintf("%.1f", l)})
			}
			_, _, wc := runWrite(conc, *warmupOps, concSweepChunk, sw.seq)
			wc()
			wlats, wallW, cleanup := runWrite(conc, *totalOps, concSweepChunk, sw.seq)
			opsW := float64(*totalOps) / wallW
			mbW := opsW * float64(concSweepChunk) / 1e6
			fmt.Printf("%-14s %-5s %6d  %8.0f  %6.1f  %7.3f  %7.3f  %7.3f  %7.3f  %8.3f\n",
				backend, "write", conc, opsW, mbW,
				mean(wlats)/1000, pct(wlats, 0.5)/1000, pct(wlats, 0.95)/1000,
				pct(wlats, 0.99)/1000, pct(wlats, 0.999)/1000)
			for _, l := range wlats {
				w.Write([]string{backend, sw.tag, "write", fmt.Sprint(conc), fmt.Sprint(concSweepChunk), fmt.Sprintf("%.1f", l)})
			}
			cleanup()
		}
	}

	// ── chunk sweeps (fixed concurrency=64, vary chunk) ──────────────────────
	type chunkSweepSpec struct {
		tag   string
		label string
		seq   bool
	}
	chunkSweeps := []chunkSweepSpec{
		{"chunk_sweep_rand", "SWEEP 2 — chunk size / random", false},
		{"chunk_sweep_seq",  "SWEEP 4 — chunk size / sequential", true},
	}
	hdrChunk := fmt.Sprintf("%-14s %-5s %6s  %8s  %6s  %7s  %7s  %7s  %7s  %8s  (ms)",
		"backend", "op", "chunk", "ops/s", "MB/s", "mean", "p50", "p95", "p99", "p999")
	for _, sw := range chunkSweeps {
		fmt.Printf("\n%s\n%s\n%s\n", sw.label, hdrChunk, sep)
		for _, chunk := range chunkSweep {
			label := fmt.Sprintf("%dK", chunk/1024)
			if chunk >= 1024*1024 {
				label = fmt.Sprintf("%dM", chunk/(1024*1024))
			}
			runRead(pool, chunkSweepConc, *warmupOps, chunk, sw.seq)
			lats, wallR := runRead(pool, chunkSweepConc, *totalOps, chunk, sw.seq)
			opsS := float64(*totalOps) / wallR
			mbS := opsS * float64(chunk) / 1e6
			fmt.Printf("%-14s %-5s %6s  %8.0f  %6.1f  %7.3f  %7.3f  %7.3f  %7.3f  %8.3f\n",
				backend, "read", label, opsS, mbS,
				mean(lats)/1000, pct(lats, 0.5)/1000, pct(lats, 0.95)/1000,
				pct(lats, 0.99)/1000, pct(lats, 0.999)/1000)
			for _, l := range lats {
				w.Write([]string{backend, sw.tag, "read", fmt.Sprint(chunkSweepConc), fmt.Sprint(chunk), fmt.Sprintf("%.1f", l)})
			}
			_, _, wc := runWrite(chunkSweepConc, *warmupOps, chunk, sw.seq)
			wc()
			wlats, wallW, cleanup := runWrite(chunkSweepConc, *totalOps, chunk, sw.seq)
			opsW := float64(*totalOps) / wallW
			mbW := opsW * float64(chunk) / 1e6
			fmt.Printf("%-14s %-5s %6s  %8.0f  %6.1f  %7.3f  %7.3f  %7.3f  %7.3f  %8.3f\n",
				backend, "write", label, opsW, mbW,
				mean(wlats)/1000, pct(wlats, 0.5)/1000, pct(wlats, 0.95)/1000,
				pct(wlats, 0.99)/1000, pct(wlats, 0.999)/1000)
			for _, l := range wlats {
				w.Write([]string{backend, sw.tag, "write", fmt.Sprint(chunkSweepConc), fmt.Sprint(chunk), fmt.Sprintf("%.1f", l)})
			}
			cleanup()
		}
	}

	w.Flush()
	outF.Close()
	fmt.Printf("\nCSV → %s\n", outPath)
}
