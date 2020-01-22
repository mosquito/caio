#include <linux/aio_abi.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/eventfd.h>
#include <sys/syscall.h>
#include <unistd.h>

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <structmember.h>


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
    int fileno;
} EventFD;


typedef struct {
    PyObject_HEAD
    aio_context_t ctx;
    unsigned max_requests;
} AIOContext;


typedef struct {
    PyObject_HEAD
    EventFD* eventfd;
    AIOContext* context;
    PyObject* py_buffer;
    char* buffer;
    struct iocb iocb;
} AIOOperation;


static PyTypeObject* AIOOperationTypeP = NULL;
static PyTypeObject* AIOContextTypeP = NULL;
static PyTypeObject* EventFDTypeP = NULL;


static void
EventFD_dealloc(EventFD *self) {
    if (self->fileno > 0) close(self->fileno);
    Py_TYPE(self)->tp_free((PyObject *) self);
}

static PyObject *
EventFD_new(PyTypeObject *type, PyObject *args, PyObject *kwds) {
    EventFD *self;

    self = (EventFD *) type->tp_alloc(type, 0);

    if (self != NULL) {
        self->fileno = eventfd(0, 0);

        if (self->fileno <= 0) {
            PyErr_SetFromErrno(PyExc_SystemError);
            return NULL;
        }
    }

    return (PyObject *) self;
}

static int EventFD_init(PyObject* self, PyObject *args, PyObject *kwds) {
    static char *kwlist[] = {NULL};
    return PyArg_ParseTupleAndKeywords(args, kwds, "", kwlist);
}

static PyMemberDef EventFD_members[] = {
    {"fileno", T_INT, offsetof(EventFD, fileno), READONLY,
     "file descritptor"},
    {NULL}  /* Sentinel */
};

static PyObject* EventFD_repr(EventFD *self) {
    return PyUnicode_FromFormat(
        "<%s as %p: fp=%i>",
        Py_TYPE(self)->tp_name, self, self->fileno
    );
}

static PyTypeObject
EventFDType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "eventfd.EventFD",
    .tp_doc = "linux EventFD descriptor",
    .tp_basicsize = sizeof(EventFD),
    .tp_itemsize = 0,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_new = EventFD_new,
    .tp_init = (initproc) EventFD_init,
    .tp_dealloc = (destructor) EventFD_dealloc,
    .tp_members = EventFD_members,
    .tp_repr = (reprfunc) EventFD_repr
};


static void
AIOContext_dealloc(AIOContext *self) {
    if (self->ctx != 0) {
        aio_context_t ctx = self->ctx;
        self->ctx = 0;

        io_destroy(ctx);
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
    self->max_requests = 0;
    self->ctx = 0;

    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|I", kwlist, &self->max_requests)) {
        return -1;
    }

    if (self->max_requests <= 0) {
        self->max_requests = 32;
    }

    if (io_setup(self->max_requests, &self->ctx) < 0) {
        PyErr_SetFromErrno(PyExc_SystemError);
        return -1;
    }

    return 0;
}

static PyObject* AIOContext_repr(AIOContext *self) {
    return PyUnicode_FromFormat(
        "<%s as %p: max_requests=%i, ctx=%lli>",
        Py_TYPE(self)->tp_name, self, self->max_requests, self->ctx
    );
}


/*
    AIOContext properties
*/
static PyMemberDef AIOContext_members[] = {
    {
        "max_requests",
        T_USHORT,
        offsetof(AIOContext, max_requests),
        READONLY,
        "max requests"
    },
    {NULL}  /* Sentinel */
};


PyDoc_STRVAR(AIOContext_submit_docstring,
    "Accepts multiple AIOOperations. Returns \n\n"
    "    AIOOpeartion.submit(aio_op1, aio_op2, aio_opN, ...) -> int"
);
static PyObject* AIOContext_submit(
    AIOContext *self, PyObject *args
) {
    if (!PyTuple_Check(args)) {
        PyErr_SetNone(PyExc_ValueError);
        return NULL;
    }

    int result = 0;

    uint32_t nr = PyTuple_Size(args);

    PyObject* obj;
    AIOOperation* op;

    struct iocb** iocbpp = PyMem_Calloc(nr, sizeof(struct iocb*));

    for (uint32_t i=0; i < nr; i++) {
        obj = PyTuple_GetItem(args, i);
        if (PyObject_TypeCheck(obj, AIOOperationTypeP) == 0) {
            PyErr_Format(
                PyExc_TypeError,
                "Wrong type for argument %d", i
            );
            return NULL;
        }

        op = (AIOOperation*) obj;
        iocbpp[i] = &op->iocb;
    }

    result = io_submit(self->ctx, nr, iocbpp);

    if (result<0) {
        PyErr_SetFromErrno(PyExc_SystemError);
        return NULL;
    }

    return (PyObject*) PyLong_FromSsize_t(result);
}

static PyMethodDef AIOContext_methods[] = {
    {
        "submit",
        (PyCFunction) AIOContext_submit, METH_VARARGS,
        AIOContext_submit_docstring
    },
    {NULL}  /* Sentinel */
};

static PyTypeObject
AIOContextType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "context.AIOContext",
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
    if (self->eventfd != NULL) {
        Py_DECREF(self->eventfd);
        self->eventfd = NULL;
    }

    if (self->context != NULL) {
        Py_DECREF(self->context);
        self->context = NULL;
    }

    if (self->iocb.aio_lio_opcode == IOCB_CMD_PREAD && self->buffer != NULL) {
        PyMem_Free(self->buffer);
        self->buffer = NULL;
    }

    if (self->py_buffer != NULL) {
        Py_DECREF(self->py_buffer);
        self->py_buffer = NULL;
    }

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
    "Creates a new instance of AIOOperation on read mode.\n\n"
    "    AIOOpeartion.read(\n"
    "        nbytes: int,\n"
    "        aio_context: AIOContext,\n"
    "        eventfd: EventFD,\n"
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

    self->context = NULL;
    self->eventfd = NULL;
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

    if (nbytes == 0) {
        Py_XDECREF(self);
        PyErr_SetString(
            PyExc_ValueError,
            "nbytes must be grater then zero"
        );
        return NULL;
    }

    self->buffer = PyMem_Calloc(nbytes, sizeof(char));
    self->iocb.aio_buf = (uint64_t) self->buffer;
    self->iocb.aio_nbytes = nbytes;
    self->py_buffer = PyMemoryView_FromMemory(self->buffer, nbytes, PyBUF_READ);

    Py_INCREF(self->py_buffer);

    self->iocb.aio_lio_opcode = IOCB_CMD_PREAD;

	return (PyObject*) self;
}

/*
    AIOOperation.write classmethod definition
*/
PyDoc_STRVAR(AIOOperation_write_docstring,
    "Creates a new instance of AIOOperation on write mode.\n\n"
    "    AIOOpeartion.write(\n"
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

    self->context = NULL;
    self->eventfd = NULL;
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

    Py_INCREF(self->py_buffer);

    self->iocb.aio_lio_opcode = IOCB_CMD_PWRITE;

    if (PyBytes_AsStringAndSize(
            self->py_buffer,
            &self->buffer,
            &nbytes
    )) {
        Py_XDECREF(self);
        return NULL;
    }

    self->iocb.aio_nbytes = nbytes;
    self->iocb.aio_buf = (uint64_t) self->buffer;

	return (PyObject*) self;
}


/*
    AIOOperation.fsync classmethod definition
*/
PyDoc_STRVAR(AIOOperation_fsync_docstring,
    "Creates a new instance of AIOOperation on fsync mode.\n\n"
    "    AIOOpeartion.fsync(\n"
    "        aio_context: AIOContext,\n"
    "        eventfd: EventFD,\n"
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

    self->context = NULL;
    self->eventfd = NULL;
    self->buffer = NULL;
    self->py_buffer = NULL;

    int argIsOk = PyArg_ParseTupleAndKeywords(
        args, kwds, "I|H", kwlist,
        &(self->iocb.aio_fildes),
        &(self->iocb.aio_reqprio)
    );

    if (!argIsOk) return NULL;

	return (PyObject*) self;
}


/*
    AIOOperation.fdsync classmethod definition
*/
PyDoc_STRVAR(AIOOperation_fdsync_docstring,
    "Creates a new instance of AIOOperation on fdsync mode.\n\n"
    "    AIOOpeartion.fdsync(\n"
    "        aio_context: AIOContext,\n"
    "        eventfd: EventFD,\n"
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

    self->buffer = NULL;
    self->py_buffer = NULL;

    int argIsOk = PyArg_ParseTupleAndKeywords(
        args, kwds, "I|H", kwlist,
        &(self->iocb.aio_fildes),
        &(self->iocb.aio_reqprio)
    );

    if (!argIsOk) return NULL;

    self->iocb.aio_lio_opcode = IOCB_CMD_FDSYNC;

	return (PyObject*) self;
}

/*
    AIOOperation.get_value method definition
*/
PyDoc_STRVAR(AIOOperation_get_value_docstring,
    "Method returns a bytes value of AIOOperation's result or None.\n\n"
    "    AIOOpeartion.get_value() -> Optional[bytes]"
);

static PyObject* AIOOperation_get_value(
    AIOOperation *self, PyObject *args, PyObject *kwds
) {
    switch (self->iocb.aio_lio_opcode) {
        case IOCB_CMD_PREAD:
            return PyBytes_FromStringAndSize(
                self->buffer, self->iocb.aio_nbytes
            );

        case IOCB_CMD_PWRITE:
            return self->py_buffer;
    }

    return NULL;
}

/*
    AIOOperation.submit method definition
*/
PyDoc_STRVAR(AIOOperation_submit_docstring,
    "Submit operation to kernel space.\n\n"
    "    AIOOpeartion.submit(aio_context, eventfd)"
);

static PyObject* AIOOperation_submit(
    AIOOperation *self, PyObject *args, PyObject *kwds
) {
    static char *kwlist[] = {"aio_context", "eventfd", NULL};

    int argIsOk = PyArg_ParseTupleAndKeywords(
        args, kwds, "OO", kwlist,
        &(self->context),
        &(self->eventfd)
    );

    if (!argIsOk) return NULL;

    if (self->context == NULL || PyObject_TypeCheck(self->context, AIOContextTypeP) == 0) {
        PyErr_SetString(
            PyExc_ValueError,
            "context argument must be instance of AIOContext class"
        );
        return NULL;
    }

    if (self->eventfd == NULL || PyObject_TypeCheck(self->eventfd, EventFDTypeP) == 0) {
        PyErr_SetString(
            PyExc_ValueError,
            "eventfd argument must be instance of EventFD class"
        );
        return NULL;
    }

    Py_INCREF(self->context);
    Py_INCREF(self->eventfd);

    self->iocb.aio_flags |= IOCB_FLAG_RESFD;
    self->iocb.aio_resfd = self->eventfd->fileno;

    int64_t context = self->context->ctx;

    struct iocb* cb = &self->iocb;
    struct iocb** cbpp = &cb;

    if (*cbpp == NULL) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "Invalid state"
        );
        return NULL;
    }

    int result = io_submit(context, 1, cbpp);
    if (result <= 0) {
        PyErr_SetFromErrno(PyExc_SystemError);
    };

    return (PyObject*) PyLong_FromSsize_t(result);
}


/*
    AIOOperation properties
*/
static PyMemberDef AIOOperation_members[] = {
    {
        "eventfd", T_OBJECT,
        offsetof(AIOOperation, eventfd),
        READONLY, "eventfd object"
    },
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
        "submit",
        (PyCFunction) AIOOperation_submit, METH_VARARGS | METH_KEYWORDS,
        AIOOperation_submit_docstring
    },
    {NULL}  /* Sentinel */
};

/*
    AIOOperation class
*/
static PyTypeObject
AIOOperationType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "linux_aio.AIOOperation",
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
    AIOContextTypeP = &AIOContextType;
    AIOOperationTypeP = &AIOOperationType;
    EventFDTypeP = &EventFDType;

    PyObject *m;

    m = PyModule_Create(&linux_aio_module);

    if (m == NULL) return NULL;

    if (PyType_Ready(EventFDTypeP) < 0) return NULL;

    Py_INCREF(EventFDTypeP);

    if (PyModule_AddObject(m, "EventFD", (PyObject *) EventFDTypeP) < 0) {
        Py_DECREF(EventFDTypeP);
        Py_DECREF(m);
        return NULL;
    }

    if (PyType_Ready(AIOContextTypeP) < 0) return NULL;

    Py_INCREF(AIOContextTypeP);

    if (PyModule_AddObject(m, "AIOContext", (PyObject *) AIOContextTypeP) < 0) {
        Py_DECREF(AIOContextTypeP);
        Py_DECREF(m);
        return NULL;
    }

    if (PyType_Ready(AIOOperationTypeP) < 0) return NULL;

    Py_INCREF(AIOOperationTypeP);

    if (PyModule_AddObject(m, "AIOOperation", (PyObject *) AIOOperationTypeP) < 0) {
        Py_DECREF(AIOOperationTypeP);
        Py_DECREF(m);
        return NULL;
    }

    return m;
}
