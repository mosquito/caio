#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

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

#include "src/threadpool/threadpool.h"


static const unsigned CTX_POOL_SIZE_DEFAULT = 8;
static const unsigned CTX_MAX_REQUESTS_DEFAULT = 512;


static PyTypeObject AIOOperationType;
static PyTypeObject AIOContextType;

typedef struct {
    PyObject_HEAD
    threadpool_t* pool;
    uint16_t max_requests;
    uint8_t pool_size;
    PyObject* weakreflist;
} AIOContext;


typedef struct {
    PyObject_HEAD
    PyObject* py_buffer;
    PyObject* callback;
    int opcode;
    unsigned int fileno;
    off_t offset;
    int result;
    uint8_t error;
    uint8_t in_progress;
    uint8_t done;   /* genuine completion reached - set via a release
                     * store by worker() before it ever touches the GIL,
                     * and read via an acquire load from payload/
                     * get_value() (both always GIL-held); this ordering
                     * is what makes result/buf_size/error - written by
                     * worker() on its own thread, without the GIL -
                     * safe to read once done is observed true. Distinct
                     * from in_progress, which is sticky forever and
                     * guards resubmission instead. */
    Py_ssize_t buf_size;
    char* buf;
    PyObject* ctx;
    PyObject* weakreflist;
} AIOOperation;


enum THAIO_OP_CODE {
    THAIO_READ,
    THAIO_WRITE,
    THAIO_FSYNC,
    THAIO_FDSYNC,
    THAIO_NOOP,
};


static PyObject *AIOOperation_callback_ref(AIOOperation *self) {
    PyObject *callback;
    CAIO_BEGIN_CRITICAL_SECTION(self);
    callback = Py_XNewRef(self->callback);
    CAIO_END_CRITICAL_SECTION();
    return callback;
}


/*
 * Stops the thread pool - shared by close() and dealloc().
 *
 * The pointer swap shares a critical section with submit()'s dispatch loop
 * (see there): a no-op under the GIL (already serialized), a real
 * per-object lock under free-threading. threadpool_destroy() runs outside
 * that section - self->pool is already NULL by then, so a concurrent
 * submit() bails immediately instead of waiting on the teardown below.
 */
static void
AIOContext_close_pool(AIOContext *self) {
    threadpool_t* pool;

    CAIO_BEGIN_CRITICAL_SECTION(self);
    pool = self->pool;
    self->pool = NULL;
    CAIO_END_CRITICAL_SECTION();

    if (pool == NULL) return;

    // Graceful: queued tasks still run instead of leaking their
    // Py_INCREF'd references. GIL released - workers need it back to run
    // their callbacks, so holding it here while joining would deadlock.
    Py_BEGIN_ALLOW_THREADS
    threadpool_destroy(pool, threadpool_graceful);
    Py_END_ALLOW_THREADS
}


static void
AIOContext_dealloc(AIOContext *self) {
    if (self->weakreflist != NULL)
        PyObject_ClearWeakRefs((PyObject *) self);

    AIOContext_close_pool(self);

    Py_TYPE(self)->tp_free((PyObject *) self);
}

/*
    AIOContext.__new__ classmethod definition
*/
static PyObject *
AIOContext_new(PyTypeObject *type, PyObject *args, PyObject *kwds) {
    AIOContext *self;

    self = (AIOContext *) type->tp_alloc(type, 0);
    return (PyObject *) self;
}

static int
AIOContext_init(AIOContext *self, PyObject *args, PyObject *kwds)
{
    static char *kwlist[] = {"max_requests", "pool_size", NULL};

    self->pool = NULL;
    self->max_requests = 0;

    if (!PyArg_ParseTupleAndKeywords(
            args, kwds, "|HH", kwlist,
            &self->max_requests, &self->pool_size
    )) return -1;

    if (self->max_requests <= 0) {
        self->max_requests = CTX_MAX_REQUESTS_DEFAULT;
    }

    if (self->pool_size <= 0) {
        self->pool_size = CTX_POOL_SIZE_DEFAULT;
    }

    if (self->pool_size > MAX_THREADS) {
        PyErr_Format(
            PyExc_ValueError,
            "pool_size too large. Allowed lower then %d",
            MAX_THREADS
        );
        return -1;
    }

    if (self->max_requests >= (MAX_QUEUE - 1)) {
        PyErr_Format(
            PyExc_ValueError,
            "max_requests too large. Allowed lower then %d",
            MAX_QUEUE - 1
        );
        return -1;
    }

    self->pool = threadpool_create(self->pool_size, self->max_requests, 0);

    if (self->pool == NULL) {
        PyErr_Format(
            PyExc_RuntimeError,
            "Pool initialization failed size=%d max_requests=%d",
            self->pool_size, self->max_requests
        );
        return -1;
    }

    return 0;
}

static PyObject* AIOContext_repr(AIOContext *self) {
    if (self->pool == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "Pool not initialized");
        return NULL;
    }
    return PyUnicode_FromFormat(
        "<%s as %p: max_requests=%i, pool_size=%i, ctx=%lli>",
        Py_TYPE(self)->tp_name, self, self->max_requests,
        self->pool_size, self->pool
    );
}


void worker(void *arg) {
    PyGILState_STATE state;

    AIOOperation* op = arg;
    PyObject* ctx = op->ctx;
    op->ctx = NULL;
    op->error = 0;

    if (op->opcode == THAIO_NOOP) {
        state = PyGILState_Ensure();
        op->ctx = NULL;
        Py_DECREF(ctx);
        Py_DECREF(op);
        PyGILState_Release(state);
        return;
    }

    int fileno = op->fileno;
    off_t offset = op->offset;
    int buf_size = op->buf_size;
    char* buf = op->buf;

    int result;

    switch (op->opcode) {
        case THAIO_WRITE:
            result = pwrite(fileno, (const char*) buf, buf_size, offset);
            break;
        case THAIO_FSYNC:
            result = fsync(fileno);
            break;
        case THAIO_FDSYNC:
#ifdef HAVE_FDATASYNC
            result = fdatasync(fileno);
#else
            result = fsync(fileno);
#endif
            break;

        case THAIO_READ:
            result = pread(fileno, buf, buf_size, offset);
            break;
    }

    op->ctx = NULL;
    op->result = result;

    if (result < 0) op->error = errno;

    if (op->opcode == THAIO_READ) {
        op->buf_size = result;
    }

    state = PyGILState_Ensure();
    if (op->opcode == THAIO_WRITE) {
        Py_CLEAR(op->py_buffer);
    }

    /* Publish completion only after every result field and Python-owned
     * buffer transition is complete. payload/get_value() acquire-load done,
     * so a reader that observes true must also observe all writes above. */
    CAIO_ATOMIC_STORE(op->done, 1);

    PyObject *callback = AIOOperation_callback_ref(op);
    if (callback != NULL) {
        PyObject *rv = PyObject_CallFunction(callback, "i", result);
        if (rv == NULL) {
            PyErr_WriteUnraisable(callback);
        } else {
            Py_DECREF(rv);
        }
        Py_DECREF(callback);
    }

    Py_DECREF(ctx);
    Py_DECREF(op);

    PyGILState_Release(state);
}


inline int process_pool_error(int code) {
    switch (code) {
        case threadpool_invalid:
            PyErr_SetString(
                PyExc_RuntimeError,
                "Thread pool pointer is invalid"
            );
            return code;
        case threadpool_lock_failure:
            PyErr_SetString(
                PyExc_RuntimeError,
                "Failed to lock thread pool"
            );
            return code;
        case threadpool_queue_full:
            PyErr_Format(
                PyExc_RuntimeError,
                "Thread pool queue full"
            );
            return code;
        case threadpool_shutdown:
            PyErr_SetString(
                PyExc_RuntimeError,
                "Thread pool is shutdown"
            );
            return code;
        case threadpool_thread_failure:
            PyErr_SetString(
                PyExc_RuntimeError,
                "Thread failure"
            );
            return code;
    }

    if (code < 0) PyErr_SetString(PyExc_RuntimeError, "Unknown error");
    return code;
}



PyDoc_STRVAR(AIOContext_submit_docstring,
    "Accepts multiple Operations. Returns \n\n"
    "    Operation.submit(aio_op1, aio_op2, aio_opN, ...) -> int"
);
static PyObject* AIOContext_submit(
    AIOContext *self, PyObject *args
) {
    if (self == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "self is NULL");
        return NULL;
    }

    if (!PyTuple_Check(args)) {
        PyErr_SetNone(PyExc_ValueError);
        return NULL;
    }

    Py_ssize_t nr = PyTuple_Size(args);
    Py_ssize_t i;
    PyObject* obj;

    // Heap-allocated rather than a stack VLA sized by the caller-controlled
    // tuple length - *ops(*a_huge_tuple) must not be able to overflow the
    // stack.
    AIOOperation** ops = NULL;
    if (nr > 0) {
        ops = PyMem_New(AIOOperation*, nr);
        if (ops == NULL) {
            PyErr_NoMemory();
            return NULL;
        }
    }

    for (i=0; i < nr; i++) {
        obj = PyTuple_GetItem(args, i);
        if (PyObject_TypeCheck(obj, &AIOOperationType) == 0) {
            PyErr_Format(
                PyExc_TypeError,
                "Wrong type for argument %zd", i
            );
            PyMem_Free(ops);
            return NULL;
        }

        ops[i] = (AIOOperation*) obj;
    }

    Py_ssize_t j=0;
    int result = 0;
    int failed = 0;

    // Shares AIOContext_close_pool()'s critical section (see there) - must
    // cover the whole loop, not just the read, or a concurrent close()
    // could destroy `pool` between the check and threadpool_add().
    CAIO_BEGIN_CRITICAL_SECTION(self);

    threadpool_t* pool = self->pool;
    if (pool == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "self->pool is NULL");
        failed = 1;
    } else {
        for (i=0; i < nr; i++) {
            // Atomic exchange, not check-then-set: two Contexts racing on
            // the same Operation must not both dispatch it to a worker.
            if (CAIO_ATOMIC_LOAD_STORE(ops[i]->in_progress, 1)) continue;

            ops[i]->ctx = (void*) self;
            Py_INCREF(ops[i]);
            Py_INCREF(self);

            result = threadpool_add(pool, worker, (void*) ops[i], 0);
            if (process_pool_error(result) < 0) {
                CAIO_ATOMIC_STORE(ops[i]->in_progress, 0);
                ops[i]->ctx = NULL;
                Py_DECREF(ops[i]);
                Py_DECREF(self);
                failed = 1;
                break;
            }
            j++;
        }
    }

    CAIO_END_CRITICAL_SECTION();

    PyMem_Free(ops);

    if (failed) return NULL;
    return (PyObject*) PyLong_FromSsize_t(j);
}


PyDoc_STRVAR(AIOContext_cancel_docstring,
    "Cancels multiple Operations. Returns \n\n"
    "    Operation.cancel(aio_op1, aio_op2, aio_opN, ...) -> int\n\n"
    "(Always returns zero, this method exists for compatibility reasons)"
);
static PyObject* AIOContext_cancel(
    AIOContext *self, PyObject *args
) {
    return (PyObject*) PyLong_FromSsize_t(0);
}


PyDoc_STRVAR(AIOContext_close_docstring,
    "Stops the native thread pool. Idempotent - a second call is a no-op.\n\n"
    "Graceful: any Operation already running or still queued gets to run "
    "to completion first. submit() after close() raises RuntimeError."
);
static PyObject* AIOContext_close(
    AIOContext *self, PyObject *Py_UNUSED(ignored)
) {
    AIOContext_close_pool(self);
    Py_RETURN_NONE;
}


/*
    AIOContext properties
*/
static PyMemberDef AIOContext_members[] = {
    {
        "pool_size",
        T_INT,
        offsetof(AIOContext, pool_size),
        READONLY,
        "pool_size"
    },
    {
        "max_requests",
        T_USHORT,
        offsetof(AIOContext, max_requests),
        READONLY,
        "max requests"
    },
    {NULL}  /* Sentinel */
};

static PyMethodDef AIOContext_methods[] = {
    {
        "submit",
        (PyCFunction) AIOContext_submit, METH_VARARGS,
        AIOContext_submit_docstring
    },
    {
        "cancel",
        (PyCFunction) AIOContext_cancel, METH_VARARGS,
        AIOContext_cancel_docstring
    },
    {
        "close",
        (PyCFunction) AIOContext_close, METH_NOARGS,
        AIOContext_close_docstring
    },
    {NULL}  /* Sentinel */
};

static PyTypeObject
AIOContextType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "Context",
    .tp_doc = "thread aio context",
    .tp_basicsize = sizeof(AIOContext),
    .tp_itemsize = 0,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_new = AIOContext_new,
    .tp_init = (initproc) AIOContext_init,
    .tp_dealloc = (destructor) AIOContext_dealloc,
    .tp_members = AIOContext_members,
    .tp_methods = AIOContext_methods,
    .tp_repr = (reprfunc) AIOContext_repr,
    .tp_weaklistoffset = offsetof(AIOContext, weakreflist)
};


static int
AIOOperation_traverse(AIOOperation *self, visitproc visit, void *arg) {
    Py_VISIT(self->callback);
    Py_VISIT(self->py_buffer);
    Py_VISIT(self->ctx);
    return 0;
}


static int
AIOOperation_clear(AIOOperation *self) {
    Py_CLEAR(self->callback);

    /* self->buf is a separate PyMem_Calloc allocation py_buffer's own
     * memoryview only views, not owns - clearing py_buffer alone would
     * leak it (and, if this runs before this Operation's own eventual
     * dealloc, that dealloc's identical free-then-NULL below prevents a
     * double free only because this already set it to NULL). */
    if ((self->opcode == THAIO_READ) && self->buf != NULL) {
        PyMem_Free(self->buf);
        self->buf = NULL;
    }

    Py_CLEAR(self->py_buffer);

    /* Normally already NULL by any point tp_clear/dealloc could run -
     * worker() clears it before ever touching the GIL again - but that's
     * only true once genuinely dispatched; cleared here too in case a
     * cyclic collection runs during the narrow submit()-to-dispatch
     * window where it's still set. */
    Py_CLEAR(self->ctx);

    return 0;
}


static void
AIOOperation_dealloc(AIOOperation *self) {
    PyObject_GC_UnTrack(self);

    if (self->weakreflist != NULL)
        PyObject_ClearWeakRefs((PyObject *) self);

    AIOOperation_clear(self);
    Py_TYPE(self)->tp_free((PyObject *) self);
}


static PyObject* AIOOperation_repr(AIOOperation *self) {
    char* mode;

    switch (self->opcode) {
        case THAIO_READ:
            mode = "read";
            break;

        case THAIO_WRITE:
            mode = "write";
            break;

        case THAIO_FSYNC:
            mode = "fsync";
            break;

        case THAIO_FDSYNC:
            mode = "fdsync";
            break;
        default:
            mode = "noop";
            break;
    }

    return PyUnicode_FromFormat(
        "<%s at %p: mode=\"%s\", fd=%i, offset=%i, result=%i, buffer=%p>",
        Py_TYPE(self)->tp_name, self, mode,
        self->fileno, self->offset, self->result, self->buf
    );
}


/*
    AIOOperation.read classmethod definition
*/
PyDoc_STRVAR(AIOOperation_read_docstring,
    "Creates a new instance of Operation on read mode.\n\n"
    "    Operation.read(\n"
    "        nbytes: int,\n"
    "        aio_context: Context,\n"
    "        fd: int, \n"
    "        offset: int,\n"
    "        priority=0\n"
    "    )"
);

static PyObject* AIOOperation_read(
    PyTypeObject *type, PyObject *args, PyObject *kwds
) {
    AIOOperation *self = (AIOOperation *) type->tp_alloc(type, 0);

    static char *kwlist[] = {"nbytes", "fd", "offset", "priority", NULL};

    if (self == NULL) {
        PyErr_SetString(PyExc_MemoryError, "can not allocate memory");
        return NULL;
    }

    self->buf = NULL;
    self->py_buffer = NULL;
    self->in_progress = 0;
    self->done = 0;
    self->weakreflist = NULL;

    uint64_t nbytes = 0;
    uint16_t priority;

    int argIsOk = PyArg_ParseTupleAndKeywords(
        args, kwds, "KI|LH", kwlist,
        &nbytes,
        &(self->fileno),
        &(self->offset),
        &priority
    );

    if (!argIsOk) {
        Py_DECREF(self);
        return NULL;
    }

    // PyMem_Calloc can return NULL for a large enough (or just
    // OOM-at-the-time) nbytes - proceeding with a NULL buf would hand the
    // kernel (via pread() in worker()) and PyMemoryView_FromMemory a NULL
    // pointer with a nonzero declared size, corrupting memory instead of
    // raising a catchable error.
    self->buf = PyMem_Calloc(nbytes, sizeof(char));
    if (self->buf == NULL && nbytes > 0) {
        Py_DECREF(self);
        PyErr_NoMemory();
        return NULL;
    }
    self->buf_size = nbytes;

    self->py_buffer = PyMemoryView_FromMemory(
        self->buf,
        self->buf_size,
        PyBUF_READ
    );

    if (self->py_buffer == NULL) {
        Py_DECREF(self);
        return NULL;
    }

    self->opcode = THAIO_READ;

	return (PyObject*) self;
}

/*
    AIOOperation.write classmethod definition
*/
PyDoc_STRVAR(AIOOperation_write_docstring,
    "Creates a new instance of Operation on write mode.\n\n"
    "    Operation.write(\n"
    "        payload_bytes: bytes,\n"
    "        fd: int, \n"
    "        offset: int,\n"
    "        priority=0\n"
    "    )"
);

static PyObject* AIOOperation_write(
    PyTypeObject *type, PyObject *args, PyObject *kwds
) {
    AIOOperation *self = (AIOOperation *) type->tp_alloc(type, 0);

    static char *kwlist[] = {"payload_bytes", "fd", "offset", "priority", NULL};

    if (self == NULL) {
        PyErr_SetString(PyExc_MemoryError, "can not allocate memory");
        return NULL;
    }

    // unused
    uint16_t priority;

    self->buf = NULL;
    self->py_buffer = NULL;
    self->in_progress = 0;
    self->done = 0;
    self->weakreflist = NULL;

    // Parsed into a plain local first, not directly into self->py_buffer:
    // "O" hands back a borrowed reference, and self->py_buffer must never
    // hold one - AIOOperation_dealloc unconditionally Py_CLEARs it. Only
    // assigned (and incref'd) below once it's confirmed to actually be the
    // bytes object this Operation is going to own.
    PyObject* payload_bytes = NULL;

    int argIsOk = PyArg_ParseTupleAndKeywords(
        args, kwds, "OI|LH", kwlist,
        &payload_bytes,
        &(self->fileno),
        &(self->offset),
        &priority
    );

    if (!argIsOk) {
        Py_DECREF(self);
        return NULL;
    }

    if (!PyBytes_Check(payload_bytes)) {
        Py_DECREF(self);
        PyErr_SetString(
            PyExc_ValueError,
            "payload_bytes argument must be bytes"
        );
        return NULL;
    }

    self->opcode = THAIO_WRITE;

    if (PyBytes_AsStringAndSize(
            payload_bytes,
            &self->buf,
            &self->buf_size
    )) {
        Py_DECREF(self);
        PyErr_SetString(
            PyExc_RuntimeError,
            "Can not convert bytes to c string"
        );
        return NULL;
    }

    self->py_buffer = payload_bytes;
    Py_INCREF(self->py_buffer);

	return (PyObject*) self;
}


/*
    AIOOperation.fsync classmethod definition
*/
PyDoc_STRVAR(AIOOperation_fsync_docstring,
    "Creates a new instance of Operation on fsync mode.\n\n"
    "    Operation.fsync(\n"
    "        aio_context: AIOContext,\n"
    "        fd: int, \n"
    "        priority=0\n"
    "    )"
);

static PyObject* AIOOperation_fsync(
    PyTypeObject *type, PyObject *args, PyObject *kwds
) {
    AIOOperation *self = (AIOOperation *) type->tp_alloc(type, 0);

    static char *kwlist[] = {"fd", "priority", NULL};

    if (self == NULL) {
        PyErr_SetString(PyExc_MemoryError, "can not allocate memory");
        return NULL;
    }

    uint16_t priority;

    self->buf = NULL;
    self->py_buffer = NULL;
    self->in_progress = 0;
    self->done = 0;
    self->weakreflist = NULL;

    int argIsOk = PyArg_ParseTupleAndKeywords(
        args, kwds, "I|H", kwlist,
        &(self->fileno),
        &priority
    );

    if (!argIsOk) {
        Py_DECREF(self);
        return NULL;
    }

    self->opcode = THAIO_FSYNC;

	return (PyObject*) self;
}


/*
    AIOOperation.fdsync classmethod definition
*/
PyDoc_STRVAR(AIOOperation_fdsync_docstring,
    "Creates a new instance of Operation on fdsync mode.\n\n"
    "    Operation.fdsync(\n"
    "        aio_context: AIOContext,\n"
    "        fd: int, \n"
    "        priority=0\n"
    "    )"
);

static PyObject* AIOOperation_fdsync(
    PyTypeObject *type, PyObject *args, PyObject *kwds
) {
    AIOOperation *self = (AIOOperation *) type->tp_alloc(type, 0);

    static char *kwlist[] = {"fd", "priority", NULL};

    if (self == NULL) {
        PyErr_SetString(PyExc_MemoryError, "can not allocate memory");
        return NULL;
    }

    self->buf = NULL;
    self->py_buffer = NULL;
    self->in_progress = 0;
    self->done = 0;
    self->weakreflist = NULL;
    uint16_t priority;

    int argIsOk = PyArg_ParseTupleAndKeywords(
        args, kwds, "I|H", kwlist,
        &(self->fileno),
        &priority
    );

    if (!argIsOk) {
        Py_DECREF(self);
        return NULL;
    }

    self->opcode = THAIO_FDSYNC;

	return (PyObject*) self;
}

/*
    AIOOperation.get_value method definition
*/
PyDoc_STRVAR(AIOOperation_get_value_docstring,
    "Method returns a bytes value of Operation's result or None.\n\n"
    "    Operation.get_value() -> Optional[bytes]"
);

static PyObject* AIOOperation_get_value(
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
        PyErr_SetString(
            PyExc_SystemError,
            strerror(self->error)
        );

        return NULL;
    }

    switch (self->opcode) {
        case THAIO_READ:
            /* self->buf can only be NULL here if tp_clear() already ran -
             * only possible once this Operation is otherwise unreachable
             * (see AIOOperation_traverse/_clear), so nothing could
             * actually be waiting on this return value, but degrade to
             * None rather than hand out an uninitialized buffer either
             * way (PyBytes_FromStringAndSize(NULL, n>0) doesn't crash,
             * it silently returns garbage). */
            if (self->buf == NULL)
                Py_RETURN_NONE;
            return PyBytes_FromStringAndSize(
                self->buf, self->buf_size
            );

        case THAIO_WRITE:
            return PyLong_FromSsize_t(self->result);
    }

    Py_RETURN_NONE;
}


/*
    AIOOperation.get_value method definition
*/
PyDoc_STRVAR(AIOOperation_set_callback_docstring,
    "Set callback which will be called after Operation will be finished.\n\n"
    "    Operation.get_value() -> Optional[bytes]"
);

static PyObject* AIOOperation_set_callback(
    AIOOperation *self, PyObject *args, PyObject *kwds
) {
    static char *kwlist[] = {"callback", NULL};

    PyObject* callback;
    PyObject* old_callback;

    int argIsOk = PyArg_ParseTupleAndKeywords(
        args, kwds, "O", kwlist,
        &callback
    );

    if (!argIsOk) return NULL;

    if (!PyCallable_Check(callback)) {
        PyErr_Format(
            PyExc_ValueError,
            "object %r is not callable",
            callback
        );
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

    /* fsync/fdsync Operations never allocate a buffer, and a completed
     * write's is freed in worker() before done is published (i.e. before
     * this getter could ever observe it) - matches T_OBJECT's (as opposed
     * to T_OBJECT_EX's) own NULL-to-None behavior, which this getter
     * replaces. */
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


/*
    AIOOperation properties
*/
static PyMemberDef AIOOperation_members[] = {
    {
        "fileno", T_UINT,
        offsetof(AIOOperation, fileno),
        READONLY, "file descriptor"
    },
    {
        "offset", T_ULONGLONG,
        offsetof(AIOOperation, offset),
        READONLY, "offset"
    },
    {
        "nbytes", T_ULONGLONG,
        offsetof(AIOOperation, buf_size),
        READONLY, "nbytes"
    },
    {
        "result", T_INT,
        offsetof(AIOOperation, result),
        READONLY, "result"
    },
    {
        "error", T_INT,
        offsetof(AIOOperation, error),
        READONLY, "error"
    },
    {NULL}  /* Sentinel */
};

/*
    AIOOperation methods
*/
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
        (PyCFunction) AIOOperation_get_value, METH_NOARGS,
        AIOOperation_get_value_docstring
    },
    {
        "set_callback",
        (PyCFunction) AIOOperation_set_callback, METH_VARARGS | METH_KEYWORDS,
        AIOOperation_set_callback_docstring
    },
    {NULL}  /* Sentinel */
};

/*
    AIOOperation class
*/
static PyTypeObject
AIOOperationType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "aio.AIOOperation",
    .tp_doc = "thread aio operation representation",
    .tp_basicsize = sizeof(AIOOperation),
    .tp_itemsize = 0,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .tp_dealloc = (destructor) AIOOperation_dealloc,
    .tp_traverse = (traverseproc) AIOOperation_traverse,
    .tp_clear = (inquiry) AIOOperation_clear,
    .tp_members = AIOOperation_members,
    .tp_getset = AIOOperation_getset,
    .tp_methods = AIOOperation_methods,
    .tp_repr = (reprfunc) AIOOperation_repr,
    .tp_weaklistoffset = offsetof(AIOOperation, weakreflist)
};


static PyModuleDef thread_aio_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "thread_aio",
    .m_doc = "Thread based AIO.",
    .m_size = -1,
};


PyMODINIT_FUNC PyInit_thread_aio(void) {
    Py_Initialize();

    PyObject *m;

    m = PyModule_Create(&thread_aio_module);

    if (m == NULL) return NULL;
    if (CAIO_DECLARE_FREE_THREADED(m) < 0) {
        Py_DECREF(m);
        return NULL;
    }

    if (PyType_Ready(&AIOContextType) < 0) return NULL;

    Py_INCREF(&AIOContextType);

    if (PyModule_AddObject(m, "Context", (PyObject *) &AIOContextType) < 0) {
        Py_XDECREF(&AIOContextType);
        Py_XDECREF(m);
        return NULL;
    }

    if (PyType_Ready(&AIOOperationType) < 0) return NULL;

    Py_INCREF(&AIOOperationType);

    if (PyModule_AddObject(m, "Operation", (PyObject *) &AIOOperationType) < 0) {
        Py_XDECREF(&AIOOperationType);
        Py_XDECREF(m);
        return NULL;
    }

    return m;
}
