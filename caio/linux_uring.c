/*
 * io_uring AIO backend for caio
 * Requires Linux 5.6+ (IORING_OP_READ / IORING_OP_WRITE without iovec)
 */

#include <errno.h>
#include <limits.h>
#include <poll.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/eventfd.h>
#include <sys/syscall.h>
#include <sys/utsname.h>
#include <signal.h>
#include <linux/io_uring.h>

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <structmember.h>

#if PY_VERSION_HEX >= 0x030D0000 && defined(Py_GIL_DISABLED)
#define CAIO_BEGIN_CRITICAL_SECTION(object) \
    PyCriticalSection caio_critical_section; \
    PyCriticalSection_Begin( \
        &caio_critical_section, (PyObject *)(object) \
    )
#define CAIO_END_CRITICAL_SECTION() \
    PyCriticalSection_End(&caio_critical_section)
#else
#define CAIO_BEGIN_CRITICAL_SECTION(object)
#define CAIO_END_CRITICAL_SECTION()
#endif

#define CAIO_ATOMIC_LOAD(value) \
    __atomic_load_n(&(value), __ATOMIC_ACQUIRE)
#define CAIO_ATOMIC_STORE(value, new_value) \
    __atomic_store_n(&(value), (new_value), __ATOMIC_RELEASE)
#define CAIO_ATOMIC_LOAD_STORE(value, new_value) \
    __atomic_exchange_n(&(value), (new_value), __ATOMIC_ACQ_REL)

#if PY_VERSION_HEX >= 0x030D0000 && defined(Py_GIL_DISABLED)
#define CAIO_DECLARE_FREE_THREADED(module) \
    PyUnstable_Module_SetGIL((module), Py_MOD_GIL_NOT_USED)
#else
#define CAIO_DECLARE_FREE_THREADED(module) 0
#endif

/* ---- syscall wrappers ---- */
static inline int io_uring_setup(uint32_t entries, struct io_uring_params *p) {
    return (int) syscall(__NR_io_uring_setup, entries, p);
}

static inline int io_uring_enter(
    int fd, uint32_t to_submit, uint32_t min_complete,
    uint32_t flags, sigset_t *sig
) {
    return (int) syscall(
        __NR_io_uring_enter, fd, to_submit, min_complete,
        flags, sig, _NSIG / 8
    );
}

static inline int io_uring_register(
    int fd, uint32_t opcode, void *arg, uint32_t nr_args
) {
    return (int) syscall(__NR_io_uring_register, fd, opcode, arg, nr_args);
}

/* ---- io_uring setup flags not always present in older kernel headers ---- */
#ifndef IORING_SETUP_SQPOLL
#define IORING_SETUP_SQPOLL         (1U << 1)   /* Linux 5.1 */
#endif
#ifndef IORING_SETUP_SINGLE_ISSUER
#define IORING_SETUP_SINGLE_ISSUER  (1U << 12)   /* Linux 6.0 */
#endif
#ifndef IORING_SETUP_NO_SQARRAY
#define IORING_SETUP_NO_SQARRAY     (1U << 16)   /* Linux 6.1 */
#endif
#ifndef IORING_SQ_NEED_WAKEUP
#define IORING_SQ_NEED_WAKEUP       (1U << 0)   /* sq_flags: kernel thread asleep */
#endif
#ifndef IORING_ENTER_SQ_WAKEUP
#define IORING_ENTER_SQ_WAKEUP      (1U << 1)   /* io_uring_enter: wake SQPOLL thread */
#endif

/* ---- Internal operation opcodes ---- */
enum URING_OP {
    URING_READ   = 0,
    URING_WRITE  = 1,
    URING_FSYNC  = 2,
    URING_FDSYNC = 3,
};

/* user_data sentinel for cancel/internal SQEs (not an AIOOperation pointer) */
#define CANCEL_USER_DATA  UINT64_MAX

static const uint32_t CTX_MAX_REQUESTS_DEFAULT = 32;
static const uint32_t EV_MAX_REQUESTS_DEFAULT  = 512;

static PyTypeObject AIOOperationType;
static PyTypeObject AIOContextType;


/* ================================================================
   AIOOperation
   ================================================================ */

typedef struct {
    PyObject_HEAD
    PyObject   *py_buffer;   /* memoryview (read) or bytes (write) */
    PyObject   *callback;
    PyObject   *context;     /* owning Context, held only while in flight */
    PyObject   *weakreflist;
    uint8_t     opcode;      /* URING_* enum */
    uint32_t    fileno;
    uint64_t    offset;
    int32_t     result;
    int32_t     error;
    Py_ssize_t  buf_size;
    char       *buf;
    uint8_t     in_progress;
    uint8_t     done;   /* genuine completion reached - unlike in_progress
                          * (sticky forever, guards resubmission), this is
                          * what payload/get_value() gate on */
} AIOOperation;


static int AIOOperation_traverse(AIOOperation *self, visitproc visit, void *arg) {
    Py_VISIT(self->py_buffer);
    Py_VISIT(self->callback);
    Py_VISIT(self->context);
    return 0;
}


static int AIOOperation_clear(AIOOperation *self) {
    Py_CLEAR(self->callback);
    Py_CLEAR(self->context);
    /* buf points into py_buffer's internal storage for reads — do NOT free
     * it separately; Py_CLEAR(py_buffer) handles the memory. */
    Py_CLEAR(self->py_buffer);
    return 0;
}


static PyObject *AIOOperation_callback_ref(AIOOperation *self) {
    PyObject *callback;
    CAIO_BEGIN_CRITICAL_SECTION(self);
    callback = Py_XNewRef(self->callback);
    CAIO_END_CRITICAL_SECTION();
    return callback;
}


static void AIOOperation_dealloc(AIOOperation *self) {
    PyObject_GC_UnTrack(self);

    if (self->weakreflist != NULL)
        PyObject_ClearWeakRefs((PyObject *) self);

    AIOOperation_clear(self);
    Py_TYPE(self)->tp_free((PyObject *) self);
}


static PyObject *AIOOperation_repr(AIOOperation *self) {
    const char *mode;
    switch (self->opcode) {
        case URING_READ:   mode = "read";   break;
        case URING_WRITE:  mode = "write";  break;
        case URING_FSYNC:  mode = "fsync";  break;
        case URING_FDSYNC: mode = "fdsync"; break;
        default:           mode = "noop";   break;
    }
    return PyUnicode_FromFormat(
        "<%s at %p: mode=\"%s\", fd=%u, offset=%llu, result=%d, buffer=%p>",
        Py_TYPE(self)->tp_name, self, mode,
        self->fileno, (unsigned long long) self->offset,
        self->result, self->buf
    );
}


PyDoc_STRVAR(AIOOperation_read_docstring,
    "Creates a new Operation for reading.\n\n"
    "    Operation.read(nbytes, fd, offset, priority=0) -> Operation"
);
static PyObject *AIOOperation_read(
    PyTypeObject *type, PyObject *args, PyObject *kwds
) {
    static char *kwlist[] = {"nbytes", "fd", "offset", "priority", NULL};

    AIOOperation *self = (AIOOperation *) type->tp_alloc(type, 0);
    if (self == NULL) {
        PyErr_SetString(PyExc_MemoryError, "cannot allocate memory");
        return NULL;
    }

    self->buf = NULL;
    self->py_buffer = NULL;
    self->in_progress = 0;
    self->done = 0;
    self->context = NULL;
    self->weakreflist = NULL;
    self->callback = NULL;
    self->error = 0;

    uint64_t nbytes = 0;
    uint16_t priority = 0;

    if (!PyArg_ParseTupleAndKeywords(
            args, kwds, "KI|KH", kwlist,
            &nbytes, &self->fileno, &self->offset, &priority)) {
        Py_DECREF(self);
        return NULL;
    }

    /* Allocate the result bytes object directly — the kernel writes into
     * its internal buffer, so get_value() can return it with no copy.
     * PyBytes_FromStringAndSize(NULL, n) leaves the memory uninitialized
     * (documented CPython behavior) - a short read's untouched tail must
     * read as zero, not whatever heap garbage happened to be there
     * (a real information-disclosure risk, not just cosmetic). */
    self->py_buffer = PyBytes_FromStringAndSize(NULL, (Py_ssize_t) nbytes);
    if (self->py_buffer == NULL) {
        Py_DECREF(self);
        return NULL;
    }
    self->buf      = PyBytes_AS_STRING(self->py_buffer);
    self->buf_size = (Py_ssize_t) nbytes;
    memset(self->buf, 0, (size_t) nbytes);
    self->opcode   = URING_READ;

    return (PyObject *) self;
}


PyDoc_STRVAR(AIOOperation_write_docstring,
    "Creates a new Operation for writing.\n\n"
    "    Operation.write(payload_bytes, fd, offset, priority=0) -> Operation"
);
static PyObject *AIOOperation_write(
    PyTypeObject *type, PyObject *args, PyObject *kwds
) {
    static char *kwlist[] = {"payload_bytes", "fd", "offset", "priority", NULL};

    AIOOperation *self = (AIOOperation *) type->tp_alloc(type, 0);
    if (self == NULL) {
        PyErr_SetString(PyExc_MemoryError, "cannot allocate memory");
        return NULL;
    }

    self->buf = NULL;
    self->py_buffer = NULL;
    self->in_progress = 0;
    self->done = 0;
    self->context = NULL;
    self->weakreflist = NULL;
    self->callback = NULL;
    self->error = 0;

    uint16_t priority = 0;

    /* Parsed into a plain local first, not directly into self->py_buffer:
     * "O" hands back a borrowed reference, and self->py_buffer must never
     * hold one - AIOOperation_dealloc unconditionally Py_CLEARs it. Only
     * assigned (and incref'd) below once confirmed to actually be the
     * bytes object this Operation is going to own. */
    PyObject *payload_bytes = NULL;

    if (!PyArg_ParseTupleAndKeywords(
            args, kwds, "OI|KH", kwlist,
            &payload_bytes, &self->fileno, &self->offset, &priority)) {
        Py_DECREF(self);
        return NULL;
    }

    if (!PyBytes_Check(payload_bytes)) {
        Py_DECREF(self);
        PyErr_SetString(PyExc_ValueError, "payload_bytes must be bytes");
        return NULL;
    }

    if (PyBytes_AsStringAndSize(payload_bytes, &self->buf, &self->buf_size)) {
        Py_DECREF(self);
        return NULL;
    }

    self->py_buffer = payload_bytes;
    Py_INCREF(self->py_buffer);
    self->opcode = URING_WRITE;

    return (PyObject *) self;
}


PyDoc_STRVAR(AIOOperation_fsync_docstring,
    "Creates a new Operation for fsync.\n\n"
    "    Operation.fsync(fd, priority=0) -> Operation"
);
static PyObject *AIOOperation_fsync(
    PyTypeObject *type, PyObject *args, PyObject *kwds
) {
    static char *kwlist[] = {"fd", "priority", NULL};

    AIOOperation *self = (AIOOperation *) type->tp_alloc(type, 0);
    if (self == NULL) {
        PyErr_SetString(PyExc_MemoryError, "cannot allocate memory");
        return NULL;
    }

    self->buf = NULL;
    self->py_buffer = NULL;
    self->in_progress = 0;
    self->done = 0;
    self->context = NULL;
    self->weakreflist = NULL;
    self->callback = NULL;
    self->error = 0;

    uint16_t priority = 0;

    if (!PyArg_ParseTupleAndKeywords(
            args, kwds, "I|H", kwlist, &self->fileno, &priority)) {
        Py_DECREF(self);
        return NULL;
    }

    self->opcode = URING_FSYNC;
    return (PyObject *) self;
}


PyDoc_STRVAR(AIOOperation_fdsync_docstring,
    "Creates a new Operation for fdatasync.\n\n"
    "    Operation.fdsync(fd, priority=0) -> Operation"
);
static PyObject *AIOOperation_fdsync(
    PyTypeObject *type, PyObject *args, PyObject *kwds
) {
    static char *kwlist[] = {"fd", "priority", NULL};

    AIOOperation *self = (AIOOperation *) type->tp_alloc(type, 0);
    if (self == NULL) {
        PyErr_SetString(PyExc_MemoryError, "cannot allocate memory");
        return NULL;
    }

    self->buf = NULL;
    self->py_buffer = NULL;
    self->in_progress = 0;
    self->done = 0;
    self->context = NULL;
    self->weakreflist = NULL;
    self->callback = NULL;
    self->error = 0;

    uint16_t priority = 0;

    if (!PyArg_ParseTupleAndKeywords(
            args, kwds, "I|H", kwlist, &self->fileno, &priority)) {
        Py_DECREF(self);
        return NULL;
    }

    self->opcode = URING_FDSYNC;
    return (PyObject *) self;
}


PyDoc_STRVAR(AIOOperation_get_value_docstring,
    "Returns the result of the completed Operation.\n\n"
    "    Operation.get_value() -> Optional[Union[bytes, int]]"
);
static PyObject *AIOOperation_get_value(
    AIOOperation *self, PyObject *args, PyObject *kwds
) {
    if (
        CAIO_ATOMIC_LOAD(self->in_progress) &&
        !CAIO_ATOMIC_LOAD(self->done)
    ) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "get_value() is not available while the operation is in flight"
        );
        return NULL;
    }

    if (self->error != 0) {
        PyErr_SetString(PyExc_SystemError, strerror(self->error));
        return NULL;
    }

    switch (self->opcode) {
        case URING_READ:
            /* py_buffer can only be NULL here if tp_clear() already ran -
             * only possible once this Operation is otherwise unreachable
             * (see AIOOperation_traverse/_clear), so nothing could actually
             * be waiting on this return value, but degrade to None rather
             * than crash on the now-dangling self->buf either way. */
            if (self->py_buffer == NULL)
                Py_RETURN_NONE;

            /* Fast path: kernel filled the whole buffer — return py_buffer
             * directly with no copy.  Partial reads (e.g. EOF) fall back to
             * a slice. */
            if (self->buf_size == PyBytes_GET_SIZE(self->py_buffer)) {
                Py_INCREF(self->py_buffer);
                return self->py_buffer;
            }
            return PyBytes_FromStringAndSize(self->buf, self->buf_size);
        case URING_WRITE:
            return PyLong_FromSsize_t(self->result);
    }

    Py_RETURN_NONE;
}


PyDoc_STRVAR(AIOOperation_set_callback_docstring,
    "Sets a callback to invoke when the Operation completes.\n\n"
    "    Operation.set_callback(callback) -> bool"
);
static PyObject *AIOOperation_set_callback(
    AIOOperation *self, PyObject *callback
) {
    PyObject *old_callback;

    if (!PyCallable_Check(callback)) {
        PyErr_Format(PyExc_ValueError, "object %r is not callable", callback);
        return NULL;
    }

    Py_INCREF(callback);
    CAIO_BEGIN_CRITICAL_SECTION(self);
    old_callback = self->callback;
    self->callback = callback;
    CAIO_END_CRITICAL_SECTION();
    Py_XDECREF(old_callback);

    Py_RETURN_TRUE;
}


static PyObject *AIOOperation_payload_getter(AIOOperation *self, void *closure) {
    if (
        CAIO_ATOMIC_LOAD(self->in_progress) &&
        !CAIO_ATOMIC_LOAD(self->done)
    ) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "payload is not available while the operation is in flight"
        );
        return NULL;
    }

    /* fsync/fdsync Operations never allocate a buffer - matches T_OBJECT's
     * (as opposed to T_OBJECT_EX's) own NULL-to-None behavior, which this
     * getter replaces. */
    if (self->py_buffer == NULL)
        Py_RETURN_NONE;

    Py_INCREF(self->py_buffer);
    return self->py_buffer;
}


static PyGetSetDef AIOOperation_getset[] = {
    {
        "payload", (getter) AIOOperation_payload_getter, NULL,
        "payload", NULL
    },
    {NULL}
};


static PyMemberDef AIOOperation_members[] = {
    {
        "context", T_OBJECT,
        offsetof(AIOOperation, context),   READONLY, "context object"
    },
    {
        "fileno",  T_UINT,
        offsetof(AIOOperation, fileno),    READONLY, "file descriptor"
    },
    {
        "offset",  T_ULONGLONG,
        offsetof(AIOOperation, offset),    READONLY, "offset"
    },
    {
        "nbytes",  T_PYSSIZET,
        offsetof(AIOOperation, buf_size),  READONLY, "nbytes"
    },
    {
        "result",  T_INT,
        offsetof(AIOOperation, result),    READONLY, "result"
    },
    {
        "error",   T_INT,
        offsetof(AIOOperation, error),     READONLY, "error"
    },
    {NULL}
};

static PyMethodDef AIOOperation_methods[] = {
    {
        "read",
        (PyCFunction) AIOOperation_read,
        METH_CLASS | METH_VARARGS | METH_KEYWORDS,
        AIOOperation_read_docstring
    },
    {
        "write",
        (PyCFunction) AIOOperation_write,
        METH_CLASS | METH_VARARGS | METH_KEYWORDS,
        AIOOperation_write_docstring
    },
    {
        "fsync",
        (PyCFunction) AIOOperation_fsync,
        METH_CLASS | METH_VARARGS | METH_KEYWORDS,
        AIOOperation_fsync_docstring
    },
    {
        "fdsync",
        (PyCFunction) AIOOperation_fdsync,
        METH_CLASS | METH_VARARGS | METH_KEYWORDS,
        AIOOperation_fdsync_docstring
    },
    {
        "get_value",
        (PyCFunction) AIOOperation_get_value,
        METH_NOARGS,
        AIOOperation_get_value_docstring
    },
    {
        "set_callback",
        (PyCFunction) AIOOperation_set_callback,
        METH_O,
        AIOOperation_set_callback_docstring
    },
    {NULL}
};

static PyTypeObject AIOOperationType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name      = "linux_uring.Operation",
    .tp_doc       = "io_uring AIO operation",
    .tp_basicsize = sizeof(AIOOperation),
    .tp_itemsize  = 0,
    .tp_flags     = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .tp_dealloc   = (destructor) AIOOperation_dealloc,
    .tp_traverse  = (traverseproc) AIOOperation_traverse,
    .tp_clear     = (inquiry) AIOOperation_clear,
    .tp_repr      = (reprfunc)   AIOOperation_repr,
    .tp_members   = AIOOperation_members,
    .tp_getset    = AIOOperation_getset,
    .tp_methods   = AIOOperation_methods,
    .tp_weaklistoffset = offsetof(AIOOperation, weakreflist),
};


/* ================================================================
   AIOContext
   ================================================================ */

typedef struct {
    PyObject_HEAD
    int      uring_fd;
    int      eventfd_fd;
    uint32_t max_requests;

    /* SQ ring mmap */
    void     *sq_ring_ptr;
    size_t    sq_ring_size;
    uint32_t *sq_head;
    uint32_t *sq_tail;
    uint32_t *sq_ring_mask;
    uint32_t *sq_ring_entries;
    uint32_t *sq_flags;
    uint32_t *sq_array;

    /* SQE array mmap */
    struct io_uring_sqe *sqes;
    size_t               sqes_size;

    /* CQ ring mmap */
    void     *cq_ring_ptr;
    size_t    cq_ring_size;
    uint32_t *cq_head;
    uint32_t *cq_tail;
    uint32_t *cq_ring_mask;
    uint32_t *cq_ring_entries;
    struct io_uring_cqe *cqes;

    uint8_t  no_sqarray;   /* IORING_SETUP_NO_SQARRAY was used */
    uint8_t  sqpoll;       /* IORING_SETUP_SQPOLL was used */

    PyObject *weakreflist;
} AIOContext;


static void AIOContext_dealloc(AIOContext *self) {
    if (self->weakreflist != NULL)
        PyObject_ClearWeakRefs((PyObject *) self);

    if (self->sq_ring_ptr != MAP_FAILED && self->sq_ring_ptr != NULL)
        munmap(self->sq_ring_ptr, self->sq_ring_size);
    if (self->sqes != MAP_FAILED && self->sqes != NULL)
        munmap(self->sqes, self->sqes_size);
    /* Skip CQ munmap when it shares the SQ ring mapping (IORING_FEAT_SINGLE_MMAP) */
    if (self->cq_ring_ptr != MAP_FAILED && self->cq_ring_ptr != NULL &&
        self->cq_ring_ptr != self->sq_ring_ptr)
        munmap(self->cq_ring_ptr, self->cq_ring_size);
    if (self->uring_fd >= 0)
        close(self->uring_fd);
    if (self->eventfd_fd >= 0)
        close(self->eventfd_fd);

    Py_TYPE(self)->tp_free((PyObject *) self);
}


static PyObject *AIOContext_new(PyTypeObject *type, PyObject *args, PyObject *kwds) {
    AIOContext *self = (AIOContext *) type->tp_alloc(type, 0);
    if (self != NULL) {
        self->weakreflist = NULL;
        self->uring_fd    = -1;
        self->eventfd_fd  = -1;
        self->sq_ring_ptr = MAP_FAILED;
        self->cq_ring_ptr = MAP_FAILED;
        self->sqes        = MAP_FAILED;
        self->no_sqarray  = 0;
        self->sqpoll      = 0;
    }
    return (PyObject *) self;
}


static int AIOContext_init(AIOContext *self, PyObject *args, PyObject *kwds) {
    static char *kwlist[] = {"max_requests", "sqpoll", NULL};

    self->max_requests = 0;
    int want_sqpoll    = 0;

    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|Ip", kwlist,
                                     &self->max_requests, &want_sqpoll))
        return -1;

    if (self->max_requests == 0)
        self->max_requests = CTX_MAX_REQUESTS_DEFAULT;

    /* eventfd for asyncio add_reader notification. EFD_NONBLOCK so poll()'s
     * read() raises BlockingIOError when nothing is pending yet, as
     * documented, instead of blocking the caller. */
    self->eventfd_fd = eventfd(0, EFD_NONBLOCK);
    if (self->eventfd_fd < 0) {
        PyErr_SetFromErrno(PyExc_SystemError);
        return -1;
    }

    /*
     * Two probe-and-fallback flag tables.
     *
     * Default (sqpoll=False): conventional explicit submit via io_uring_enter.
     *   flush() → io_uring_enter() → inline completions land immediately in CQ
     *   → drain_cq fires futures before `await`, so page-cache ops skip
     *   event-loop suspension entirely.  Latency ~ linux_aio at low QD.
     *
     * Opt-in (sqpoll=True): SQPOLL kernel thread polls SQ ring; io_uring_enter
     *   only needed to wake a sleeping thread.  Eliminates per-op syscall
     *   overhead at sustained high QD.  EPERM on pre-5.11 kernels without
     *   CAP_SYS_NICE is treated like EINVAL (try next entry).
     *
     * IORING_SETUP_SINGLE_ISSUER deliberately never tried: it pins the ring
     * to whichever thread's io_uring_setup()/io_uring_enter() call created
     * it, rejecting submit()/flush() from any other thread with -EEXIST -
     * this Context's own Python API makes no such thread-affinity promise.
     */
    static const uint32_t flag_table_default[] = {
        IORING_SETUP_NO_SQARRAY,
        0,
    };
    static const uint32_t flag_table_sqpoll[] = {
        IORING_SETUP_SQPOLL | IORING_SETUP_NO_SQARRAY,
        IORING_SETUP_SQPOLL,
        0,  /* fallback: plain ring without SQPOLL */
    };

    const uint32_t *flag_table = want_sqpoll ? flag_table_sqpoll : flag_table_default;
    int table_len = want_sqpoll
        ? (int)(sizeof(flag_table_sqpoll) / sizeof(flag_table_sqpoll[0]))
        : (int)(sizeof(flag_table_default) / sizeof(flag_table_default[0]));

    struct io_uring_params params;
    uint32_t flags_used = 0;

    for (int i = 0; i < table_len; i++) {
        memset(&params, 0, sizeof(params));
        params.flags = flag_table[i];
        if (params.flags & IORING_SETUP_SQPOLL)
            params.sq_thread_idle = 100;  /* ms; let kthread sleep when idle */
        /* +1: SQPOLL dequeue lag headroom */
        self->uring_fd = io_uring_setup(self->max_requests + 1, &params);
        if (self->uring_fd >= 0) {
            flags_used = params.flags;
            break;
        }
        if (errno != EINVAL && errno != EPERM) {
            PyErr_SetFromErrno(PyExc_SystemError);
            return -1;
        }
    }
    if (self->uring_fd < 0) {
        PyErr_SetFromErrno(PyExc_SystemError);
        return -1;
    }
    self->no_sqarray = (flags_used & IORING_SETUP_NO_SQARRAY) ? 1 : 0;
    self->sqpoll     = (flags_used & IORING_SETUP_SQPOLL)     ? 1 : 0;

    /*
     * IORING_FEAT_SINGLE_MMAP: SQ ring and CQ ring share one mmap at
     * IORING_OFF_SQ_RING; use max(sq_size, cq_size) for the mapping.
     * Without it, use two separate mmaps (original behaviour).
     */
    size_t sq_ring_size, cq_ring_size;
    if (self->no_sqarray)
        sq_ring_size = params.sq_off.flags + sizeof(uint32_t);
    else
        sq_ring_size = params.sq_off.array + params.sq_entries * sizeof(uint32_t);
    cq_ring_size = params.cq_off.cqes + params.cq_entries * sizeof(struct io_uring_cqe);

    if (params.features & IORING_FEAT_SINGLE_MMAP) {
        self->sq_ring_size = sq_ring_size > cq_ring_size ? sq_ring_size : cq_ring_size;
        self->sq_ring_ptr  = mmap(
            NULL, self->sq_ring_size,
            PROT_READ | PROT_WRITE, MAP_SHARED | MAP_POPULATE,
            self->uring_fd, IORING_OFF_SQ_RING
        );
        if (self->sq_ring_ptr == MAP_FAILED) {
            PyErr_SetFromErrno(PyExc_SystemError);
            return -1;
        }
        self->cq_ring_ptr  = self->sq_ring_ptr; /* shared mapping */
        self->cq_ring_size = 0;                 /* don't munmap separately */
    } else {
        self->sq_ring_size = sq_ring_size;
        self->sq_ring_ptr  = mmap(
            NULL, self->sq_ring_size,
            PROT_READ | PROT_WRITE, MAP_SHARED | MAP_POPULATE,
            self->uring_fd, IORING_OFF_SQ_RING
        );
        if (self->sq_ring_ptr == MAP_FAILED) {
            PyErr_SetFromErrno(PyExc_SystemError);
            return -1;
        }
        self->cq_ring_size = cq_ring_size;
        self->cq_ring_ptr  = mmap(
            NULL, self->cq_ring_size,
            PROT_READ | PROT_WRITE, MAP_SHARED | MAP_POPULATE,
            self->uring_fd, IORING_OFF_CQ_RING
        );
        if (self->cq_ring_ptr == MAP_FAILED) {
            PyErr_SetFromErrno(PyExc_SystemError);
            return -1;
        }
    }

    char *sq = (char *) self->sq_ring_ptr;
    self->sq_head         = (uint32_t *)(sq + params.sq_off.head);
    self->sq_tail         = (uint32_t *)(sq + params.sq_off.tail);
    self->sq_ring_mask    = (uint32_t *)(sq + params.sq_off.ring_mask);
    self->sq_ring_entries = (uint32_t *)(sq + params.sq_off.ring_entries);
    self->sq_flags        = (uint32_t *)(sq + params.sq_off.flags);
    self->sq_array        = (uint32_t *)(sq + params.sq_off.array); /* unused when no_sqarray */

    /* mmap SQE array */
    self->sqes_size = params.sq_entries * sizeof(struct io_uring_sqe);
    self->sqes = mmap(
        NULL, self->sqes_size,
        PROT_READ | PROT_WRITE, MAP_SHARED | MAP_POPULATE,
        self->uring_fd, IORING_OFF_SQES
    );
    if (self->sqes == MAP_FAILED) {
        PyErr_SetFromErrno(PyExc_SystemError);
        return -1;
    }

    char *cq = (char *) self->cq_ring_ptr;
    self->cq_head         = (uint32_t *)(cq + params.cq_off.head);
    self->cq_tail         = (uint32_t *)(cq + params.cq_off.tail);
    self->cq_ring_mask    = (uint32_t *)(cq + params.cq_off.ring_mask);
    self->cq_ring_entries = (uint32_t *)(cq + params.cq_off.ring_entries);
    self->cqes = (struct io_uring_cqe *)(cq + params.cq_off.cqes);

    /*
     * Register eventfd for completion notification, unconditionally with
     * plain IORING_REGISTER_EVENTFD (not the _ASYNC variant) in both
     * SQPOLL and non-SQPOLL mode.
     *
     * Non-SQPOLL mode used IORING_REGISTER_EVENTFD_ASYNC previously, to
     * avoid a redundant _on_read_event wakeup for ops that already
     * completed inline during io_uring_enter (caught by drain_cq() inside
     * flush() before the eventfd path would even run). That relies on the
     * kernel's own inline-vs-deferred classification for whether to signal
     * the fd - confirmed (via a genuine disk-backed repro, not tmpfs) to
     * fail to signal at all for at least one real deferred completion, an
     * unresolvable hang: the read reaches the CQ ring but nothing ever
     * wakes epoll_wait() to drain it. A spurious extra wakeup on an
     * already-fully-drained ring is harmless (process_events() just finds
     * nothing and returns immediately); a completion notification that
     * silently never arrives is not. Same reasoning applies to SQPOLL,
     * which already used the plain variant for a similar reason (the
     * SQPOLL thread's own synchronous completions must not be suppressed
     * either).
     */
    if (io_uring_register(self->uring_fd, IORING_REGISTER_EVENTFD,
                          &self->eventfd_fd, 1) < 0) {
        PyErr_SetFromErrno(PyExc_SystemError);
        return -1;
    }

    return 0;
}


static PyObject *AIOContext_repr(AIOContext *self) {
    return PyUnicode_FromFormat(
        "<%s as %p: max_requests=%u, uring_fd=%d, eventfd=%d, sqpoll=%d>",
        Py_TYPE(self)->tp_name, self,
        self->max_requests, self->uring_fd, self->eventfd_fd,
        (int) self->sqpoll
    );
}


/*
 * Drain all currently-available CQEs from the completion ring.
 * No syscall — reads directly from the mmap'd ring.
 * Returns number of completions processed, or -1 on Python error.
 *
 * The whole batch (up to `max` real completions) is scanned and the ring's
 * own head is fully committed BEFORE any callback runs - a callback that
 * reentrantly calls process_events()/flush() must see a ring already
 * caught up through this entire batch, never a subset of it. Committing
 * head incrementally, one CQE at a time right before that CQE's own
 * callback, does not fix this: by the time callback #3 (say) runs, head
 * would already be past #3, but a reentrant call would then race the
 * outer loop to process #4..N itself, double-processing whichever ones
 * both reach first - the exact same class of bug, just shifted to a
 * different subset. Collecting the whole batch first and only invoking
 * callbacks afterward removes the race entirely.
 */
static int uring_drain_cq(AIOContext *self, uint32_t max) {
    CAIO_BEGIN_CRITICAL_SECTION(self);  /* released before any callback runs */

    uint32_t head = __atomic_load_n(self->cq_head, __ATOMIC_RELAXED);
    uint32_t tail = __atomic_load_n(self->cq_tail, __ATOMIC_ACQUIRE);
    uint32_t mask = *self->cq_ring_mask;

    uint32_t avail = tail - head;
    uint32_t cap = avail < max ? avail : max;

    AIOOperation **ops = NULL;
    int32_t *results = NULL;
    if (cap > 0) {
        ops = PyMem_New(AIOOperation *, cap);
        results = PyMem_New(int32_t, cap);
        if (ops == NULL || results == NULL) {
            PyMem_Free(ops);
            PyMem_Free(results);
            CAIO_END_CRITICAL_SECTION();
            PyErr_NoMemory();
            return -1;
        }
    }

    uint32_t count = 0;
    while (head != tail && count < max) {
        struct io_uring_cqe *cqe = &self->cqes[head & mask];
        head++;

        if (cqe->user_data == CANCEL_USER_DATA)
            continue;

        /* in_progress deliberately NOT reset here - one-shot forever once
         * genuinely submitted, matching thread_aio/linux_aio and the
         * (paused) Rust rewrite: a completed Operation must not be
         * resubmittable, only a fresh one constructed for a retry.
         * context IS released here (unlike in_progress) - the kernel is
         * done touching anything for this op, so there's no more reason
         * to keep the Context alive on its behalf. */
        AIOOperation *op = (AIOOperation *)(uintptr_t) cqe->user_data;
        op->result = cqe->res;
        if (cqe->res < 0) {
            op->error = -cqe->res;
        } else if (op->opcode == URING_READ) {
            op->buf_size = cqe->res;
        }
        Py_CLEAR(op->context);
        CAIO_ATOMIC_STORE(op->done, 1);

        ops[count] = op;
        results[count] = cqe->res;
        count++;
    }

    /* Ring state fully committed - reentrant callers now see this whole
     * batch as already consumed, before a single callback has run. */
    __atomic_store_n(self->cq_head, head, __ATOMIC_RELEASE);
    CAIO_END_CRITICAL_SECTION();

    for (uint32_t i = 0; i < count; i++) {
        AIOOperation *op = ops[i];
        PyObject *callback = AIOOperation_callback_ref(op);

        if (callback != NULL) {
            PyObject *arg = PyLong_FromLong((long) results[i]);
            if (arg == NULL) {
                PyErr_WriteUnraisable(callback);
            } else {
                PyObject *rv = PyObject_CallOneArg(callback, arg);
                Py_DECREF(arg);
                if (rv == NULL) {
                    PyErr_WriteUnraisable(callback);
                } else {
                    Py_DECREF(rv);
                }
            }
            Py_DECREF(callback);
        }

        Py_DECREF(op);
    }

    PyMem_Free(ops);
    PyMem_Free(results);
    return (int) count;
}


PyDoc_STRVAR(AIOContext_submit_docstring,
    "Submits Operations to the io_uring SQ.\n\n"
    "    Context.submit(op1, op2, ...) -> int"
);
static PyObject *AIOContext_submit(AIOContext *self, PyObject *args) {
    if (self->uring_fd < 0) {
        PyErr_SetString(PyExc_RuntimeError, "context not initialized");
        return NULL;
    }

    if (!PyTuple_Check(args)) {
        PyErr_SetNone(PyExc_ValueError);
        return NULL;
    }

    Py_ssize_t nr = PyTuple_GET_SIZE(args);
    if (nr == 0)
        return PyLong_FromLong(0);

    for (Py_ssize_t i = 0; i < nr; i++) {
        PyObject *obj = PyTuple_GET_ITEM(args, i);
        if (Py_TYPE(obj) != &AIOOperationType) {
            PyErr_Format(PyExc_TypeError, "argument %zd is not an Operation", i);
            return NULL;
        }
    }

    CAIO_BEGIN_CRITICAL_SECTION(self);  /* serializes tail/SQE writes */

    uint32_t tail     = __atomic_load_n(self->sq_tail, __ATOMIC_RELAXED);
    uint32_t head     = __atomic_load_n(self->sq_head, __ATOMIC_ACQUIRE);
    uint32_t mask     = *self->sq_ring_mask;
    uint32_t capacity = *self->sq_ring_entries;
    uint32_t submitted = 0;

    for (Py_ssize_t i = 0; i < nr; i++) {
        AIOOperation *op = (AIOOperation *) PyTuple_GET_ITEM(args, i);

        /* Atomic exchange, not check-then-set: two Contexts racing on the same
         * Operation must not both stage an SQE for it. Claimed up front
         * so the check-and-set is one atomic step - the exit paths below
         * that can still skip a freshly-claimed op undo the claim. */
        if (CAIO_ATOMIC_LOAD_STORE(op->in_progress, 1)) continue;

        if ((tail - head) >= capacity) {
            /* Not staged - give the claim back. Commit whatever WAS
             * staged earlier in this call first, or it'd be invisible to
             * the kernel forever (sq_tail never advanced past it). */
            CAIO_ATOMIC_STORE(op->in_progress, 0);
            __atomic_store_n(self->sq_tail, tail, __ATOMIC_RELEASE);
            CAIO_END_CRITICAL_SECTION();
            PyErr_SetString(PyExc_OverflowError, "io_uring SQ ring full");
            return NULL;
        }

        uint32_t index = tail & mask;
        struct io_uring_sqe *sqe = &self->sqes[index];
        memset(sqe, 0, sizeof(*sqe));

        sqe->fd        = (int32_t) op->fileno;
        sqe->off       = op->offset;
        sqe->user_data = (uint64_t)(uintptr_t) op;

        switch (op->opcode) {
            case URING_READ:
                sqe->opcode = IORING_OP_READ;
                sqe->addr   = (uint64_t)(uintptr_t) op->buf;
                sqe->len    = (uint32_t) op->buf_size;
                break;
            case URING_WRITE:
                sqe->opcode = IORING_OP_WRITE;
                sqe->addr   = (uint64_t)(uintptr_t) op->buf;
                sqe->len    = (uint32_t) op->buf_size;
                break;
            case URING_FSYNC:
                sqe->opcode = IORING_OP_FSYNC;
                break;
            case URING_FDSYNC:
                sqe->opcode       = IORING_OP_FSYNC;
                sqe->fsync_flags  = IORING_FSYNC_DATASYNC;
                break;
            default:
                /* Unrecognized opcode: give the claim back, this op was
                 * never staged. */
                CAIO_ATOMIC_STORE(op->in_progress, 0);
                continue;
        }

        if (!self->no_sqarray)
            self->sq_array[index] = index;
        tail++;

        Py_INCREF(op);

        /* Held only while genuinely in flight (cleared on completion in
         * uring_drain_cq()) - without this, a Context the caller drops
         * while an operation is still outstanding could be garbage
         * collected mid-flight, munmapping the SQ/CQ rings and closing
         * uring_fd while the kernel may still be touching them. */
        Py_XDECREF(op->context);
        op->context = (PyObject *) self;
        Py_INCREF(self);

        submitted++;
    }

    __atomic_store_n(self->sq_tail, tail, __ATOMIC_RELEASE);
    CAIO_END_CRITICAL_SECTION();

    /*
     * Do NOT call io_uring_enter here.  The Python asyncio layer batches
     * all SQEs written during the current event-loop tick and submits them
     * with a single io_uring_enter via Context.flush().  This trades
     * N per-op syscalls for one batched call, matching linux_aio's
     * io_submit(ctx, N, iocbpp) pattern.
     */
    return PyLong_FromUnsignedLong(submitted);
}


PyDoc_STRVAR(AIOContext_flush_docstring,
    "Submit all pending SQEs to the kernel in one io_uring_enter call,\n"
    "then drain any completions that finished inline.\n\n"
    "    Context.flush() -> int   # number of completions processed"
);
static PyObject *AIOContext_flush(AIOContext *self, PyObject *args) {
    if (self->uring_fd < 0) {
        PyErr_SetString(PyExc_RuntimeError, "context not initialized");
        return NULL;
    }

    /*
     * Number of SQEs filled but not yet handed to the kernel:
     *   sq_tail  = next slot we will write (updated by submit())
     *   sq_head  = next slot the kernel will consume (updated by kernel
     *              after io_uring_enter returns)
     * Their difference is the number of pending SQEs.
     */
    if (self->sqpoll) {
        /*
         * SQPOLL mode: the kernel thread picks up SQEs from the ring
         * automatically — no io_uring_enter needed for submission.
         * We only send a wakeup when the thread has gone to sleep
         * (IORING_SQ_NEED_WAKEUP set after sq_thread_idle ms of quiet).
         */
        uint32_t sq_flags = __atomic_load_n(self->sq_flags, __ATOMIC_RELAXED);
        if (sq_flags & IORING_SQ_NEED_WAKEUP) {
            if (io_uring_enter(self->uring_fd, 0, 0,
                               IORING_ENTER_SQ_WAKEUP, NULL) < 0) {
                PyErr_SetFromErrno(PyExc_SystemError);
                return NULL;
            }
        }
        int count = uring_drain_cq(self, UINT32_MAX);
        if (count < 0)
            return NULL;
        return PyLong_FromLong(count);
    }

    /* Non-SQPOLL: submit all pending SQEs with one io_uring_enter. */
    uint32_t tail      = __atomic_load_n(self->sq_tail, __ATOMIC_RELAXED);
    uint32_t head      = __atomic_load_n(self->sq_head, __ATOMIC_ACQUIRE);
    uint32_t to_submit = tail - head;

    if (to_submit == 0)
        return PyLong_FromLong(0);

    uint32_t remaining = to_submit;
    while (remaining > 0) {
        int ret = io_uring_enter(self->uring_fd, remaining, 0, 0, NULL);
        if (ret < 0) {
            PyErr_SetFromErrno(PyExc_SystemError);
            return NULL;
        }
        remaining -= (uint32_t) ret;
    }

    /*
     * Drain any completions that finished inline within io_uring_enter.
     * For page-cache hits this fires futures immediately; truly async ops
     * will arrive later via the eventfd → _on_read_event path.
     */
    int count = uring_drain_cq(self, UINT32_MAX);
    if (count < 0)
        return NULL;

    return PyLong_FromLong(count);
}


PyDoc_STRVAR(AIOContext_cancel_docstring,
    "Requests async cancellation of a submitted Operation.\n\n"
    "    Context.cancel(operation) -> int"
);
static PyObject *AIOContext_cancel(
    AIOContext *self, PyObject *args, PyObject *kwds
) {
    static char *kwlist[] = {"operation", NULL};
    AIOOperation *op = NULL;

    if (!PyArg_ParseTupleAndKeywords(
            args, kwds, "O!", kwlist, &AIOOperationType, &op))
        return NULL;

    CAIO_BEGIN_CRITICAL_SECTION(self);

    uint32_t tail  = __atomic_load_n(self->sq_tail, __ATOMIC_RELAXED);
    uint32_t head  = __atomic_load_n(self->sq_head, __ATOMIC_ACQUIRE);
    if ((tail - head) >= *self->sq_ring_entries) {
        CAIO_END_CRITICAL_SECTION();
        PyErr_SetString(PyExc_OverflowError, "io_uring SQ ring full");
        return NULL;
    }
    uint32_t index = tail & *self->sq_ring_mask;

    struct io_uring_sqe *sqe = &self->sqes[index];
    memset(sqe, 0, sizeof(*sqe));
    sqe->opcode    = IORING_OP_ASYNC_CANCEL;
    sqe->addr      = (uint64_t)(uintptr_t) op;  /* target op's user_data */
    sqe->user_data = CANCEL_USER_DATA;           /* sentinel: skip in process_events */

    if (!self->no_sqarray)
        self->sq_array[index] = index;
    __atomic_store_n(self->sq_tail, tail + 1, __ATOMIC_RELEASE);
    CAIO_END_CRITICAL_SECTION();

    io_uring_enter(self->uring_fd, 1, 0, 0, NULL);

    return PyLong_FromLong(0);
}


PyDoc_STRVAR(AIOContext_process_events_docstring,
    "Collects completed operations and fires their callbacks.\n\n"
    "    Context.process_events(max_requests=512, min_requests=0, timeout=0) -> int\n\n"
    "    timeout=0 checks once and returns immediately (the usual asyncio-\n"
    "    adapter path - real waiting happens via the eventfd/loop.add_reader()\n"
    "    instead). timeout>0 blocks up to that many seconds; timeout<0 blocks\n"
    "    indefinitely. Both are for manual synchronous polling from a plain\n"
    "    thread - do not also select()/poll() on `.fileno` from elsewhere\n"
    "    while relying on this to wait, the two waiting mechanisms are\n"
    "    alternatives, not meant to be combined on the same Context."
);
static PyObject *AIOContext_process_events(
    AIOContext *self, PyObject *args, PyObject *kwds
) {
    static char *kwlist[] = {"max_requests", "min_requests", "timeout", NULL};

    uint32_t max_requests = 0;
    uint32_t min_requests = 0;
    int32_t  tv_sec = 0;

    if (!PyArg_ParseTupleAndKeywords(
            args, kwds, "|IIi", kwlist,
            &max_requests, &min_requests, &tv_sec))
        return NULL;

    if (max_requests == 0)
        max_requests = EV_MAX_REQUESTS_DEFAULT;

    if (min_requests > max_requests) {
        PyErr_Format(
            PyExc_ValueError,
            "min_requests (%u) must be <= max_requests (%u)",
            min_requests, max_requests
        );
        return NULL;
    }

    /* Wait for at least min_requests completions: timeout=0 checks once,
     * timeout>0 bounds the wait, timeout<0 waits indefinitely. Plain
     * io_uring_enter(GETEVENTS) has no timeout parameter of its own (unlike
     * linux_aio's io_getevents, which takes a struct timespec directly) -
     * `tv_sec` used to be parsed and then silently discarded entirely, so
     * this used to always block indefinitely regardless of the argument.
     * GIL released throughout so this doesn't freeze every other thread in
     * the interpreter for the duration. */
    if (min_requests > 0 && tv_sec == 0) {
        /* Fast path: timeout=0 means "check once, don't actually wait" -
         * matching linux_aio's own io_getevents(timeout={0,0}) semantics -
         * so skip the deadline/poll-loop machinery below entirely. */
        int ret;
        Py_BEGIN_ALLOW_THREADS
        ret = io_uring_enter(self->uring_fd, 0, 0, IORING_ENTER_GETEVENTS, NULL);
        Py_END_ALLOW_THREADS
        if (ret < 0 && errno != EINTR) {
            PyErr_SetFromErrno(PyExc_SystemError);
            return NULL;
        }
    } else if (min_requests > 0) {
        int has_deadline = tv_sec > 0;
        struct timespec deadline;
        if (has_deadline) {
            clock_gettime(CLOCK_MONOTONIC, &deadline);
            deadline.tv_sec += tv_sec;
        }

        int saved_errno = 0;
        Py_BEGIN_ALLOW_THREADS
        for (;;) {
            uint32_t head = __atomic_load_n(self->cq_head, __ATOMIC_RELAXED);
            uint32_t tail = __atomic_load_n(self->cq_tail, __ATOMIC_ACQUIRE);
            if (tail - head >= min_requests)
                break;

            int ret = io_uring_enter(
                self->uring_fd, 0, 0, IORING_ENTER_GETEVENTS, NULL
            );
            if (ret < 0 && errno != EINTR) {
                saved_errno = errno;
                break;
            }

            head = __atomic_load_n(self->cq_head, __ATOMIC_RELAXED);
            tail = __atomic_load_n(self->cq_tail, __ATOMIC_ACQUIRE);
            if (tail - head >= min_requests)
                break;

            int poll_timeout_ms = -1;   /* poll()'s own "block indefinitely" */
            if (has_deadline) {
                struct timespec now;
                clock_gettime(CLOCK_MONOTONIC, &now);
                int64_t remaining_ms = (int64_t) (deadline.tv_sec - now.tv_sec) * 1000
                    + (deadline.tv_nsec - now.tv_nsec) / 1000000;
                if (remaining_ms <= 0)
                    break;
                poll_timeout_ms = remaining_ms > INT_MAX ? INT_MAX : (int) remaining_ms;
            }

            /* Block on the eventfd (the same fd loop.add_reader() watches
             * for the asyncio adapter) instead of busy-polling with a fixed
             * sleep interval - the kernel signals it the instant a
             * completion posts, so this wakes up immediately rather than up
             * to one poll interval late. A fixed-interval nanosleep() here
             * previously meant every wait effectively cost at least one
             * full interval, regardless of how quickly the completion
             * actually arrived - severe at low concurrency, where each
             * operation pays that cost on its own.
             *
             * This blocking path and the asyncio adapter's own eventfd
             * wakeup are alternative ways to wait, not meant to be combined
             * on the same Context (see the docstring) - so no separate
             * defense against racing some other waiter's read() of the
             * counter is needed here beyond what's already true of any
             * multi-waiter poll()/epoll on one fd (all current waiters see
             * readiness once the counter goes above zero; whoever's read()
             * clears it doesn't retroactively un-wake anyone who already
             * observed it). */
            struct pollfd pfd = { .fd = self->eventfd_fd, .events = POLLIN };
            int pret = poll(&pfd, 1, poll_timeout_ms);
            if (pret < 0 && errno != EINTR) {
                saved_errno = errno;
                break;
            }
            if (pret > 0 && (pfd.revents & POLLIN)) {
                uint64_t val;
                /* EFD_NONBLOCK: a short/failed read here just means
                 * something else already drained the counter - harmless,
                 * the loop re-checks the ring regardless. */
                if (read(self->eventfd_fd, &val, sizeof(val)) < 0) {
                    /* nothing to do - see comment above */
                }
            }
        }
        Py_END_ALLOW_THREADS

        if (saved_errno != 0) {
            errno = saved_errno;
            PyErr_SetFromErrno(PyExc_SystemError);
            return NULL;
        }
    }

    int count = uring_drain_cq(self, max_requests);
    if (count < 0)
        return NULL;

    return PyLong_FromLong(count);
}


PyDoc_STRVAR(AIOContext_poll_docstring,
    "Drains the eventfd counter (call before process_events in add_reader handler).\n\n"
    "    Context.poll() -> int"
);
static PyObject *AIOContext_poll(AIOContext *self, PyObject *args) {
    uint64_t result = 0;
    ssize_t  n      = read(self->eventfd_fd, &result, sizeof(uint64_t));

    if (n != (ssize_t) sizeof(uint64_t)) {
        PyErr_SetNone(PyExc_BlockingIOError);
        return NULL;
    }

    return PyLong_FromUnsignedLongLong(result);
}


static PyMemberDef AIOContext_members[] = {
    {
        "fileno",       T_INT,
        offsetof(AIOContext, eventfd_fd),   READONLY, "eventfd file descriptor"
    },
    {
        "max_requests", T_UINT,
        offsetof(AIOContext, max_requests), READONLY, "max requests"
    },
    {
        "sqpoll", T_UBYTE,
        offsetof(AIOContext, sqpoll), READONLY, "SQPOLL mode active"
    },
    {NULL}
};

static PyMethodDef AIOContext_methods[] = {
    {
        "submit",
        (PyCFunction) AIOContext_submit,
        METH_VARARGS,
        AIOContext_submit_docstring
    },
    {
        "flush",
        (PyCFunction) AIOContext_flush,
        METH_NOARGS,
        AIOContext_flush_docstring
    },
    {
        "cancel",
        (PyCFunction) AIOContext_cancel,
        METH_VARARGS | METH_KEYWORDS,
        AIOContext_cancel_docstring
    },
    {
        "process_events",
        (PyCFunction) AIOContext_process_events,
        METH_VARARGS | METH_KEYWORDS,
        AIOContext_process_events_docstring
    },
    {
        "poll",
        (PyCFunction) AIOContext_poll,
        METH_NOARGS,
        AIOContext_poll_docstring
    },
    {NULL}
};

static PyTypeObject AIOContextType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name      = "Context",
    .tp_doc       = "io_uring AIO context",
    .tp_basicsize = sizeof(AIOContext),
    .tp_itemsize  = 0,
    .tp_flags     = Py_TPFLAGS_DEFAULT,
    .tp_new       = AIOContext_new,
    .tp_init      = (initproc) AIOContext_init,
    .tp_dealloc   = (destructor) AIOContext_dealloc,
    .tp_repr      = (reprfunc) AIOContext_repr,
    .tp_members   = AIOContext_members,
    .tp_methods   = AIOContext_methods,
    .tp_weaklistoffset = offsetof(AIOContext, weakreflist),
};


/* ================================================================
   Module
   ================================================================ */

static PyModuleDef linux_uring_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "linux_uring",
    .m_doc  = "io_uring based AIO backend (Linux 5.6+).",
    .m_size = -1,
};


PyMODINIT_FUNC PyInit_linux_uring(void) {
    /* Verify kernel >= 5.6 (IORING_OP_READ / IORING_OP_WRITE) */
    struct utsname uts;
    if (uname(&uts) != 0) {
        PyErr_SetString(PyExc_ImportError, "uname() failed");
        return NULL;
    }

    int major = 0, minor = 0;
    sscanf(uts.release, "%d.%d", &major, &minor);

    if (!(major > 5 || (major == 5 && minor >= 6))) {
        PyErr_Format(
            PyExc_ImportError,
            "linux_uring requires Linux 5.6+ (IORING_OP_READ/WRITE), "
            "current kernel is %s",
            uts.release
        );
        return NULL;
    }

    /* Quick probe: ensure io_uring syscall is available */
    struct io_uring_params probe;
    memset(&probe, 0, sizeof(probe));
    int probe_fd = io_uring_setup(1, &probe);
    if (probe_fd < 0) {
        int saved_errno = errno;

        if (saved_errno == ENOSYS) {
            /* ENOSYS most commonly means a seccomp filter is blocking the
             * io_uring syscalls (the default Docker/Podman/OrbStack profile
             * blocks them).  Less commonly it means an older kernel that
             * pre-dates io_uring, which the version check above should have
             * already caught. */
            PyErr_Format(
                PyExc_ImportError,
                "io_uring_setup failed: syscall not available (ENOSYS). "
                "This usually means a seccomp filter is blocking io_uring. "
                "To fix:\n"
                "  Docker/Podman : add --security-opt seccomp=unconfined\n"
                "  Kubernetes    : set securityContext.seccompProfile.type=Unconfined\n"
                "  OrbStack      : use a plain Linux VM instead of a container context\n"
                "Kernel: %s",
                uts.release
            );
        } else {
            PyErr_Format(
                PyExc_ImportError,
                "io_uring_setup probe failed: %s",
                strerror(saved_errno)
            );
        }
        return NULL;
    }
    close(probe_fd);

    /* Probe whether SQPOLL is actually usable (kernel + capabilities) -
     * exposed as SQPOLL_ALLOWED so callers can pick a default without
     * constructing a real Context just to find out. Context(sqpoll=True)
     * already falls back gracefully on its own either way (see
     * flag_table_sqpoll) - this is purely informational. */
    struct io_uring_params sqpoll_probe;
    memset(&sqpoll_probe, 0, sizeof(sqpoll_probe));
    sqpoll_probe.flags = IORING_SETUP_SQPOLL;
    sqpoll_probe.sq_thread_idle = 100;
    int sqpoll_probe_fd = io_uring_setup(1, &sqpoll_probe);
    int sqpoll_allowed = sqpoll_probe_fd >= 0;
    if (sqpoll_probe_fd >= 0)
        close(sqpoll_probe_fd);

    PyObject *m = PyModule_Create(&linux_uring_module);
    if (m == NULL)
        return NULL;
    if (CAIO_DECLARE_FREE_THREADED(m) < 0) {
        Py_DECREF(m);
        return NULL;
    }

    if (PyModule_AddObject(m, "SQPOLL_ALLOWED", PyBool_FromLong(sqpoll_allowed)) < 0) {
        Py_DECREF(m);
        return NULL;
    }

    if (PyType_Ready(&AIOContextType)   < 0) return NULL;
    if (PyType_Ready(&AIOOperationType) < 0) return NULL;

    Py_INCREF(&AIOContextType);
    if (PyModule_AddObject(m, "Context", (PyObject *) &AIOContextType) < 0) {
        Py_DECREF(&AIOContextType);
        Py_DECREF(m);
        return NULL;
    }

    Py_INCREF(&AIOOperationType);
    if (PyModule_AddObject(m, "Operation", (PyObject *) &AIOOperationType) < 0) {
        Py_DECREF(&AIOOperationType);
        Py_DECREF(m);
        return NULL;
    }

    return m;
}
