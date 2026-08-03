//! Raw Linux AIO kernel ABI (`linux/aio_abi.h`) and syscall wrappers.
//!
//! glibc doesn't wrap these syscalls (there's no `io_setup`/`io_submit`/etc.
//! in libc), so - just like the C implementation this replaces - we go
//! through `libc::syscall` directly with the kernel-defined struct layouts.
//! This ABI has been stable since Linux 2.6 and is little-endian-only in
//! practice for the architectures this crate ships on (x86_64, aarch64).

use std::io;

pub type AioContextT = u64;

pub const IOCB_CMD_PREAD: u16 = 0;
pub const IOCB_CMD_PWRITE: u16 = 1;
pub const IOCB_CMD_FSYNC: u16 = 2;
pub const IOCB_CMD_FDSYNC: u16 = 3;

pub const IOCB_FLAG_RESFD: u32 = 1 << 0;

#[repr(C)]
#[derive(Clone, Copy)]
pub struct IoEvent {
    pub data: u64,
    pub obj: u64,
    pub res: i64,
    pub res2: i64,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct Iocb {
    pub aio_data: u64,
    pub aio_key: u32,
    pub aio_rw_flags: i32,
    pub aio_lio_opcode: u16,
    pub aio_reqprio: i16,
    pub aio_fildes: u32,
    pub aio_buf: u64,
    pub aio_nbytes: u64,
    pub aio_offset: i64,
    aio_reserved2: u64,
    pub aio_flags: u32,
    pub aio_resfd: u32,
}

impl Iocb {
    pub fn zeroed() -> Self {
        // SAFETY: an all-zero Iocb is a valid bit pattern (all plain
        // integer fields).
        unsafe { std::mem::zeroed() }
    }
}

fn check(result: libc::c_long) -> io::Result<libc::c_long> {
    if result < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(result)
    }
}

pub fn io_setup(nr_events: u32) -> io::Result<AioContextT> {
    let mut ctx: AioContextT = 0;
    unsafe {
        check(libc::syscall(
            libc::SYS_io_setup,
            nr_events as libc::c_long,
            &mut ctx as *mut AioContextT,
        ))?;
    }
    Ok(ctx)
}

pub fn io_destroy(ctx: AioContextT) -> io::Result<()> {
    unsafe {
        check(libc::syscall(libc::SYS_io_destroy, ctx))?;
    }
    Ok(())
}

/// # Safety
/// Every pointer in `iocbpp` must point to a valid, live `Iocb` for the
/// duration of this call (the kernel reads them synchronously here, then
/// asynchronously until each completes).
pub unsafe fn io_submit(ctx: AioContextT, iocbpp: &[*mut Iocb]) -> io::Result<usize> {
    let result = check(libc::syscall(
        libc::SYS_io_submit,
        ctx,
        iocbpp.len() as libc::c_long,
        iocbpp.as_ptr(),
    ))?;
    Ok(result as usize)
}

/// # Safety
/// `iocb` must be a live in-flight request previously passed to `io_submit`
/// on this same `ctx`.
pub unsafe fn io_cancel(ctx: AioContextT, iocb: *mut Iocb) -> io::Result<IoEvent> {
    let mut event = std::mem::zeroed::<IoEvent>();
    check(libc::syscall(
        libc::SYS_io_cancel,
        ctx,
        iocb,
        &mut event as *mut IoEvent,
    ))?;
    Ok(event)
}

pub fn io_getevents(
    ctx: AioContextT,
    min_nr: usize,
    max_nr: usize,
    timeout: Option<libc::timespec>,
) -> io::Result<Vec<IoEvent>> {
    // Fallible allocation instead of `vec![x; max_nr]`, which aborts the
    // whole process (not a catchable panic) on failure - `max_nr` comes
    // straight from the caller-controlled `process_events(max_requests=)`
    // argument.
    let mut events = Vec::new();
    events
        .try_reserve_exact(max_nr)
        .map_err(|e| io::Error::new(io::ErrorKind::OutOfMemory, e.to_string()))?;
    events.resize(max_nr, IoEvent { data: 0, obj: 0, res: 0, res2: 0 });

    // A bounded wait can be interrupted by any signal (e.g. SIGCHLD from
    // an unrelated multiprocessing pool) - the syscall then returns
    // -EINTR without waiting out the timeout, which must not surface as
    // an error. The deadline is computed once up front so repeated
    // EINTRs retry with the *remaining* time, not the full timeout again.
    // `checked_add` rather than `+`: the caller already rejects a
    // negative timeout, but this is the one place an overflowing deadline
    // would actually panic (`Instant + Duration` panics on overflow).
    let deadline = match timeout {
        Some(ts) => {
            let duration = std::time::Duration::new(ts.tv_sec as u64, ts.tv_nsec as u32);
            let deadline = std::time::Instant::now().checked_add(duration).ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidInput, format!("timeout {ts:?} is too large to represent"))
            })?;
            Some(deadline)
        }
        None => None,
    };

    let result = loop {
        let mut ts = deadline.map(|d| {
            let remaining = d.saturating_duration_since(std::time::Instant::now());
            libc::timespec { tv_sec: remaining.as_secs() as libc::time_t, tv_nsec: remaining.subsec_nanos() as _ }
        });
        let ts_ptr = ts.as_mut().map(|t| t as *mut libc::timespec).unwrap_or(std::ptr::null_mut());

        let outcome = unsafe {
            check(libc::syscall(
                libc::SYS_io_getevents,
                ctx,
                min_nr as libc::c_long,
                max_nr as libc::c_long,
                events.as_mut_ptr(),
                ts_ptr,
            ))
        };
        match outcome {
            Ok(n) => break n,
            Err(e) if e.raw_os_error() == Some(libc::EINTR) => continue,
            Err(e) => return Err(e),
        }
    };

    events.truncate(result as usize);
    Ok(events)
}
