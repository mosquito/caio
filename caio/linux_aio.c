#include <linux/aio_abi.h>
#include <linux/fs.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/eventfd.h>
#include <sys/syscall.h>
#include <unistd.h>

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <structmember.h>
#include <sys/utsname.h>


static const unsigned CTX_MAX_REQUESTS_DEFAULT = 32;
static const unsigned EV_MAX_REQUESTS_DEFAULT = 512;
static int fsync_support = -1;

inline int io_setup(unsigned nr, aio_context_t *ctxp) {
    return syscall(__NR_io_setup, nr, ctxp);
}


inline int io_destroy(aio_context_t ctx) {
    return syscall(__NR_io_destroy, ctx);
}


inline int io_getevents(aio_context_t ctx, long min_nr, long max_nr,
        struct io_event *events, struct timespec *timeout) {
    return syscall(__NR_io_getevents, ctx, min_nr, max_nr, events, timeout);
}


inline int io_submit(aio_context_t ctx, long nr, struct iocb **iocbpp) {
    return syscall(__NR_io_submit, ctx, nr, iocbpp);
}


typedef struct {
    PyObject_HEAD
    aio_context_t ctx;
    int fileno;
    unsigned max_requests;
} AIOContext;


typedef struct {
    PyObject_HEAD
    AIOContext* context;
    PyObject* py_buffer;
    PyObject* callback;
    char* buffer;
    int error;
    struct iocb iocb;
} AIOOperation;


static PyTypeObject* AIOOperationTypeP = NULL;
static PyTypeObject* AIOContextTypeP = NULL;


static void
AIOContext_dealloc(AIOContext *self) {
    if (self->ctx != 0) {
        aio_context_t ctx = self->ctx;
        self->ctx = 0;

        io_destroy(ctx);
    }

    if (self->fileno >= 0) {
        close(self->fileno);
        self->fileno = -1;
    }

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
    static char *kwlist[] = {"max_requests", NULL};

    self->ctx = 0;
    self->max_requests = 0;
    self->fileno = eventfd(0, 0);

    if (self->fileno < 0) {
        PyErr_SetFromErrno(PyExc_SystemError);
        return -1;
    }

    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|H", kwlist, &self->max_requests)) {
        return -1;
    }

    if (self->max_requests <= 0) {
        self->max_requests = CTX_MAX_REQUESTS_DEFAULT;
    }

    if (io_setup(self->max_requests, &self->ctx) < 0) {
        PyErr_SetFromErrno(PyExc_SystemError);
        return -1;
    }

    return 0;
}

static PyObject* AIOContext_repr(AIOContext *self) {
    if (self->ctx == 0) {
        PyErr_SetNone(PyExc_RuntimeError);
        return NULL;
    }
    return PyUnicode_FromFormat(
        "<%s as %p: max_requests=%i, ctx=%lli>",
        Py_TYPE(self)->tp_name, self, self->max_requests, self->ctx
    );
}



PyDoc_STRVAR(AIOContext_submit_docstring,
    "Accepts multiple Operations. Returns \n\n"
    "    Operation.submit(aio_op1, aio_op2, aio_opN, ...) -> int"
);
static PyObject* AIOContext_submit(AIOContext *self, PyObject *args) {
    if (self == 0) {
        PyErr_SetString(PyExc_RuntimeError, "self is NULL");
        return NULL;
    }

    if (self->ctx == 0) {
        PyErr_SetString(PyExc_RuntimeError, "self->ctx is NULL");
        return NULL;
    }

    if (!PyTuple_Check(args)) {
        PyErr_SetNone(PyExc_ValueError);
        return NULL;
    }

    int result = 0;

    uint32_t nr = PyTuple_Size(args);

    PyObject* obj;
    AIOOperation* op;

    struct iocb** iocbpp = PyMem_Calloc(nr, sizeof(struct iocb*));
    uint32_t i;

    for (i=0; i < nr; i++) {
        obj = PyTuple_GetItem(args, i);
        if (PyObject_TypeCheck(obj, AIOOperationTypeP) == 0) {
            PyErr_Format(
                PyExc_TypeError,
                "Wrong type for argument %d", i
            );
            PyMem_Free(iocbpp);
            return NULL;
        }

        op = (AIOOperation*) obj;

        op->context = self;
        Py_INCREF(self);

        Py_INCREF(op);

        op->iocb.aio_flags |= IOCB_FLAG_RESFD;
        op->iocb.aio_resfd = self->fileno;

        iocbpp[i] = &op->iocb;
    }

    result = io_submit(self->ctx, nr, iocbpp);

    if (result < 0) {
        PyErr_SetFromErrno(PyExc_SystemError);
        return NULL;
    }

    PyMem_Free(iocbpp);

    return (PyObject*) PyLong_FromSsize_t(result);
}

PyDoc_STRVAR(AIOContext_process_events_docstring,
    "Gather events for Context. \n\n"
    "    Operation.process_events(max_events, min_events) -> Tuple[Tuple[]]"
);
static PyObject* AIOContext_process_events(
    AIOContext *self, PyObject *args, PyObject *kwds
) {
    if (self->ctx == 0) {
        PyErr_SetNone(PyExc_RuntimeError);
        return NULL;
    }

    unsigned min_requests = 0;
    unsigned max_requests = 0;
    struct timespec timeout = {0, 0};

    static char *kwlist[] = {"max_requests", "min_requests", "timeout", NULL};

    if (!PyArg_ParseTupleAndKeywords(
        args, kwds, "|HHi", kwlist,
        &max_requests, &min_requests, &timeout.tv_sec
    )) { return NULL; }

    if (max_requests == 0) {
        max_requests = EV_MAX_REQUESTS_DEFAULT;
    }

    if (min_requests > max_requests) {
        PyErr_Format(
            PyExc_ValueError,
            "min_requests \"%d\" must be lower then max_requests \"%d\"",
            min_requests, max_requests
        );
        return NULL;
    }

    struct io_event events[max_requests];

    int result = io_getevents(
        self->ctx,
        min_requests,
        max_requests,
        events,
        &timeout
    );

    if (result < 0) {
        PyErr_SetFromErrno(PyExc_SystemError);
        return NULL;
    }

    AIOOperation* op;
    struct io_event* ev;

    int32_t i;
    for (i = 0; i < result; i++) {
        ev = &events[i];

        op = (AIOOperation*) ev->data;
        if (ev->res >= 0) {
            op->iocb.aio_nbytes = ev->res;
        } else {
            op->error = -ev->res;
        }

        if (op->callback == NULL) {
            continue;
        }

        if (PyObject_CallFunction(op->callback, "K", ev->res) == NULL) {
            return NULL;
        }

        Py_XDECREF(op);
    }

    return (PyObject*) PyLong_FromSsize_t(i);
}


PyDoc_STRVAR(AIOContext_poll_docstring,
        "Read value from context file descriptor.\n\n"
        "    Context().poll() -> int"
        );
static PyObject* AIOContext_poll(
    AIOContext *self, PyObject *args
) {
    if (self->ctx == 0) {
        PyErr_SetNone(PyExc_RuntimeError);
        return NULL;
    }

    if (self->fileno < 0) {
        PyErr_SetNone(PyExc_RuntimeError);
        return NULL;
    }

    uint64_t result = 0;
    int size = read(self->fileno, &result, sizeof(uint64_t));

    if (size != sizeof(uint64_t)) {
        PyErr_SetNone(PyExc_BlockingIOError);
        return NULL;
    }

    return (PyObject*) PyLong_FromUnsignedLongLong(result);
}


/*
   AIOContext properties
   */
static PyMemberDef AIOContext_members[] = {
    {
        "fileno",
        T_INT,
        offsetof(AIOContext, fileno),
        READONLY,
        "fileno"
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
        "process_events",
        (PyCFunction) AIOContext_process_events, METH_VARARGS | METH_KEYWORDS,
        AIOContext_process_events_docstring
    },
    {
        "poll",
        (PyCFunction) AIOContext_poll, METH_NOARGS,
        AIOContext_poll_docstring
    },
    {NULL}  /* Sentinel */
};

static PyTypeObject
AIOContextType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "Context",
    .tp_doc = "linux aio context representation",
    .tp_basicsize = sizeof(AIOContext),
    .tp_itemsize = 0,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_new = AIOContext_new,
    .tp_init = (initproc) AIOContext_init,
    .tp_dealloc = (destructor) AIOContext_dealloc,
    .tp_members = AIOContext_members,
    .tp_methods = AIOContext_methods,
    .tp_repr = (reprfunc) AIOContext_repr
};


static void
AIOOperation_dealloc(AIOOperation *self) {
    Py_CLEAR(self->context);
    Py_CLEAR(self->callback);

    if (self->iocb.aio_lio_opcode == IOCB_CMD_PREAD && self->buffer != NULL) {
        PyMem_Free(self->buffer);
        self->buffer = NULL;
    }

    Py_CLEAR(self->py_buffer);
    Py_TYPE(self)->tp_free((PyObject *) self);
}


static PyObject* AIOOperation_repr(AIOOperation *self) {
    char* mode;

    switch (self->iocb.aio_lio_opcode) {
        case IOCB_CMD_PREAD:
            mode = "read";
            break;

        case IOCB_CMD_PWRITE:
            mode = "write";
            break;

        case IOCB_CMD_FSYNC:
            mode = "fsync";
            break;

        case IOCB_CMD_FDSYNC:
            mode = "fdsync";
            break;
        default:
            mode = "noop";
            break;
    }

    return PyUnicode_FromFormat(
        "<%s at %p: mode=\"%s\", fd=%i, offset=%i, buffer=%p>",
        Py_TYPE(self)->tp_name, self, mode,
        self->iocb.aio_fildes, self->iocb.aio_offset, self->iocb.aio_buf
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

    memset(&self->iocb, 0, sizeof(struct iocb));

    self->iocb.aio_data = self;
    self->context = NULL;
    self->buffer = NULL;
    self->py_buffer = NULL;

    uint64_t nbytes = 0;

    int argIsOk = PyArg_ParseTupleAndKeywords(
        args, kwds, "KI|LH", kwlist,
        &nbytes,
        &(self->iocb.aio_fildes),
        &(self->iocb.aio_offset),
        &(self->iocb.aio_reqprio)
    );

    if (!argIsOk) return NULL;

    self->buffer = PyMem_Calloc(nbytes, sizeof(char));
    self->iocb.aio_buf = (uint64_t) self->buffer;
    self->iocb.aio_nbytes = nbytes;
    self->py_buffer = PyMemoryView_FromMemory(self->buffer, nbytes, PyBUF_READ);
    self->iocb.aio_lio_opcode = IOCB_CMD_PREAD;

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

    memset(&self->iocb, 0, sizeof(struct iocb));

    self->iocb.aio_data = self;

    self->context = NULL;
    self->buffer = NULL;
    self->py_buffer = NULL;

    Py_ssize_t nbytes = 0;

    int argIsOk = PyArg_ParseTupleAndKeywords(
        args, kwds, "OI|LH", kwlist,
        &(self->py_buffer),
        &(self->iocb.aio_fildes),
        &(self->iocb.aio_offset),
        &(self->iocb.aio_reqprio)
    );

    if (!argIsOk) return NULL;

    if (!PyBytes_Check(self->py_buffer)) {
        Py_XDECREF(self);
        PyErr_SetString(
            PyExc_ValueError,
            "payload_bytes argument must be bytes"
        );
        return NULL;
    }

    self->iocb.aio_lio_opcode = IOCB_CMD_PWRITE;

    if (PyBytes_AsStringAndSize(
        self->py_buffer,
        &self->buffer,
        &nbytes
    )) {
        Py_XDECREF(self);
        PyErr_SetString(
            PyExc_RuntimeError,
            "Can not convert bytes to c string"
        );
        return NULL;
    }

    Py_INCREF(self->py_buffer);

    self->iocb.aio_nbytes = nbytes;
    self->iocb.aio_buf = (uint64_t) self->buffer;

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

    memset(&self->iocb, 0, sizeof(struct iocb));

    self->iocb.aio_data = self;
    self->context = NULL;
    self->buffer = NULL;
    self->py_buffer = NULL;

    int argIsOk = PyArg_ParseTupleAndKeywords(
        args, kwds, "I|H", kwlist,
        &(self->iocb.aio_fildes),
        &(self->iocb.aio_reqprio)
    );

    if (!argIsOk) return NULL;

    if (fsync_support) {
        self->iocb.aio_lio_opcode = IOCB_CMD_FSYNC;
    }

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

    memset(&self->iocb, 0, sizeof(struct iocb));

    self->iocb.aio_data = self;
    self->buffer = NULL;
    self->py_buffer = NULL;

    int argIsOk = PyArg_ParseTupleAndKeywords(
            args, kwds, "I|H", kwlist,
            &(self->iocb.aio_fildes),
            &(self->iocb.aio_reqprio)
            );

    if (!argIsOk) return NULL;

    if (fsync_support) {
        self->iocb.aio_lio_opcode = IOCB_CMD_FDSYNC;
    }

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

    if (self->error != 0) {
        PyErr_SetString(
            PyExc_SystemError,
            strerror(self->error)
        );

        return NULL;
    }

    switch (self->iocb.aio_lio_opcode) {
        case IOCB_CMD_PREAD:
            return PyBytes_FromStringAndSize(
                self->buffer, self->iocb.aio_nbytes
            );

        case IOCB_CMD_PWRITE:
            return PyLong_FromSsize_t(self->iocb.aio_nbytes);
    }

    return Py_None;
}


/*
   AIOOperation.set_callback method definition
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
    self->callback = callback;

    Py_RETURN_TRUE;
}

/*
   AIOOperation properties
   */
static PyMemberDef AIOOperation_members[] = {
    {
        "context", T_OBJECT,
        offsetof(AIOOperation, context),
        READONLY, "context object"
    },
    {
        "fileno", T_UINT,
        offsetof(AIOOperation, iocb.aio_fildes),
        READONLY, "file descriptor"
    },
    {
        "priority", T_USHORT,
        offsetof(AIOOperation, iocb.aio_reqprio),
        READONLY, "request priority"
    },
    {
        "offset", T_ULONGLONG,
        offsetof(AIOOperation, iocb.aio_offset),
        READONLY, "offset"
    },
    {
        "payload", T_OBJECT,
        offsetof(AIOOperation, py_buffer),
        READONLY, "payload"
    },
    {
        "nbytes", T_ULONGLONG,
        offsetof(AIOOperation, iocb.aio_nbytes),
        READONLY, "nbytes"
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
    .tp_doc = "linux aio operation representation",
    .tp_basicsize = sizeof(AIOOperation),
    .tp_itemsize = 0,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_dealloc = (destructor) AIOOperation_dealloc,
    .tp_members = AIOOperation_members,
    .tp_methods = AIOOperation_methods,
    .tp_repr = (reprfunc) AIOOperation_repr
};


static PyModuleDef linux_aio_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "linux_aio",
    .m_doc = "Linux AIO c API bindings.",
    .m_size = -1,
};


PyMODINIT_FUNC PyInit_linux_aio(void) {
    struct utsname uname_data;

    if (uname(&uname_data)) {
        PyErr_SetString(PyExc_ImportError, "Can not detect linux kernel version");
        return NULL;
    }

    int release[2] = {0};
    sscanf(uname_data.release, "%d.%d", &release[0], &release[1]);

    fsync_support = (release[0] > 4) || (release[0] == 4 && release[1] >= 18);

    if (!fsync_support) {
        PyErr_WarnFormat(
            PyExc_RuntimeWarning, 0,
            "Linux supports fsync/fdsync with io_submit since 4.18 but current "
            "kernel %s doesn't support it. Related calls will have no effect.",
            uname_data.release
        );
    }

    AIOContextTypeP = &AIOContextType;
    AIOOperationTypeP = &AIOOperationType;

    PyObject *m;

    m = PyModule_Create(&linux_aio_module);

    if (m == NULL) return NULL;

    if (PyType_Ready(AIOContextTypeP) < 0) return NULL;

    Py_INCREF(AIOContextTypeP);

    if (PyModule_AddObject(m, "Context", (PyObject *) AIOContextTypeP) < 0) {
        Py_XDECREF(AIOContextTypeP);
        Py_XDECREF(m);
        return NULL;
    }

    if (PyType_Ready(AIOOperationTypeP) < 0) return NULL;

    Py_INCREF(AIOOperationTypeP);

    if (PyModule_AddObject(m, "Operation", (PyObject *) AIOOperationTypeP) < 0) {
        Py_XDECREF(AIOOperationTypeP);
        Py_XDECREF(m);
        return NULL;
    }

    return m;
}

