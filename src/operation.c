#include <stdio.h>
#include <stdlib.h>

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <structmember.h>


#include <linux/aio_abi.h>
const size_t iocb_size = sizeof(struct iocb);


PyObject *EventFDClass = NULL;
PyObject *ContextClass = NULL;


typedef struct {
    PyObject_HEAD
    PyObject* eventfd;
    PyObject* context;
    PyObject* py_buffer;
    int64_t raw_context;
    char* buffer;
    struct iocb iocb;
} AIOOperation;


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
        free(self->buffer);
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
        "<%s at %p: mode=\"%s\", fd=%i, offset=%i>",
        Py_TYPE(self)->tp_name, self, mode,
        self->iocb.aio_fildes, self->iocb.aio_offset
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

    static char *kwlist[] = {
        "nbytes", "aio_context", "eventfd", "fd", "offset", "priority",
        NULL
    };

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
        args, kwds, "KOOI|LH", kwlist,
        &nbytes,
        &(self->context),
        &(self->eventfd),
        &(self->iocb.aio_fildes),
        &(self->iocb.aio_offset),
        &(self->iocb.aio_reqprio)
    );

    if (!argIsOk) return NULL;

    if (self->context == NULL || PyObject_IsInstance(self->context, ContextClass) == 0) {
        Py_XDECREF(self);
        PyErr_SetString(
            PyExc_ValueError,
            "context argument must be instance of AIOContext class"
        );
        return NULL;
    }

    if (self->eventfd == NULL || PyObject_IsInstance(self->eventfd, EventFDClass) == 0) {
        Py_XDECREF(self);
        PyErr_SetString(
            PyExc_ValueError,
            "eventfd argument must be instance of EventFD class"
        );
        return NULL;
    }

    if (nbytes == 0) {
        Py_XDECREF(self);
        PyErr_SetString(
            PyExc_ValueError,
            "nbytes must be grater then zero"
        );
        return NULL;
    }

    self->buffer = calloc(nbytes, sizeof(char));
    self->iocb.aio_buf = (uint64_t) self->buffer;
    self->py_buffer = PyMemoryView_FromMemory(self->buffer, nbytes, PyBUF_READ);

    Py_INCREF(self->context);
    Py_INCREF(self->eventfd);
    Py_INCREF(self->py_buffer);

    self->iocb.aio_flags = IOCB_FLAG_RESFD;

    self->iocb.aio_resfd = PyLong_AsLongLong(
        PyObject_GetAttrString(self->eventfd, "fileno")
    );

    self->raw_context = PyLong_AsLongLong(
        PyObject_GetAttrString(self->context, "value")
    );

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
    "        aio_context: AIOContext,\n"
    "        eventfd: EventFD,\n"
    "        fd: int, \n"
    "        offset: int,\n"
    "        priority=0\n"
    "    )"
);

static PyObject* AIOOperation_write(
    PyTypeObject *type, PyObject *args, PyObject *kwds
) {
    AIOOperation *self = (AIOOperation *) type->tp_alloc(type, 0);

    static char *kwlist[] = {
        "payload_bytes", "aio_context", "eventfd", "fd", "offset", "priority",
        NULL
    };

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
        args, kwds, "OOOI|LH", kwlist,
        &(self->py_buffer),
        &(self->context),
        &(self->eventfd),
        &(self->iocb.aio_fildes),
        &(self->iocb.aio_offset),
        &(self->iocb.aio_reqprio)
    );

    if (!argIsOk) return NULL;

    if (self->context == NULL || PyObject_IsInstance(self->context, ContextClass) == 0) {
        Py_XDECREF(self);
        PyErr_SetString(
            PyExc_ValueError,
            "context argument must be instance of AIOContext class"
        );
        return NULL;
    }

    if (self->eventfd == NULL || PyObject_IsInstance(self->eventfd, EventFDClass) == 0) {
        Py_XDECREF(self);
        PyErr_SetString(
            PyExc_ValueError,
            "eventfd argument must be instance of EventFD class"
        );
        return NULL;
    }

    if (!PyBytes_Check(self->py_buffer)) {
        Py_XDECREF(self);
        PyErr_SetString(
            PyExc_ValueError,
            "payload_bytes argument must be bytes"
        );
        return NULL;
    }

    Py_INCREF(self->context);
    Py_INCREF(self->eventfd);
    Py_INCREF(self->py_buffer);

    self->iocb.aio_flags = IOCB_FLAG_RESFD;

    self->iocb.aio_resfd = PyLong_AsLongLong(
        PyObject_GetAttrString(self->eventfd, "fileno")
    );

    self->raw_context = PyLong_AsLongLong(
        PyObject_GetAttrString(self->context, "value")
    );

    self->iocb.aio_lio_opcode = IOCB_CMD_PWRITE;
    self->iocb.aio_reqprio = 0;

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

    static char *kwlist[] = {
        "aio_context", "eventfd", "fd", "priority",
        NULL
    };

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
        args, kwds, "OOI|H", kwlist,
        &(self->context),
        &(self->eventfd),
        &(self->iocb.aio_fildes),
        &(self->iocb.aio_reqprio)
    );

    if (!argIsOk) return NULL;

    if (self->context == NULL || PyObject_IsInstance(self->context, ContextClass) == 0) {
        Py_XDECREF(self);
        PyErr_SetString(
            PyExc_ValueError,
            "context argument must be instance of AIOContext class"
        );
        return NULL;
    }

    if (self->eventfd == NULL || PyObject_IsInstance(self->eventfd, EventFDClass) == 0) {
        Py_XDECREF(self);
        PyErr_SetString(
            PyExc_ValueError,
            "eventfd argument must be instance of EventFD class"
        );
        return NULL;
    }

    Py_INCREF(self->context);
    Py_INCREF(self->eventfd);

    self->iocb.aio_flags = IOCB_FLAG_RESFD;
    self->iocb.aio_resfd = PyLong_AsLongLong(
        PyObject_GetAttrString(self->eventfd, "fileno")
    );

    self->raw_context = PyLong_AsLongLong(
        PyObject_GetAttrString(self->context, "value")
    );

    self->iocb.aio_lio_opcode = IOCB_CMD_FSYNC;

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

    static char *kwlist[] = {
        "aio_context", "eventfd", "fd", "priority",
        NULL
    };

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
        args, kwds, "OOI|H", kwlist,
        &(self->context),
        &(self->eventfd),
        &(self->iocb.aio_fildes),
        &(self->iocb.aio_reqprio)
    );

    if (!argIsOk) return NULL;

    if (self->context == NULL || PyObject_IsInstance(self->context, ContextClass) == 0) {
        Py_XDECREF(self);
        PyErr_SetString(
            PyExc_ValueError,
            "context argument must be instance of AIOContext class"
        );
        return NULL;
    }

    if (self->eventfd == NULL || PyObject_IsInstance(self->eventfd, EventFDClass) == 0) {
        Py_XDECREF(self);
        PyErr_SetString(
            PyExc_ValueError,
            "eventfd argument must be instance of EventFD class"
        );
        return NULL;
    }

    Py_INCREF(self->context);
    Py_INCREF(self->eventfd);

    self->iocb.aio_flags = IOCB_FLAG_RESFD;
    self->iocb.aio_resfd = PyLong_AsLongLong(
        PyObject_GetAttrString(self->eventfd, "fileno")
    );

    self->raw_context = PyLong_AsLongLong(
        PyObject_GetAttrString(self->context, "value")
    );

    self->iocb.aio_lio_opcode = IOCB_CMD_FDSYNC;

	return (PyObject*) self;
}

/*
    AIOOperation.get_value classmethod definition
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


/*
    MODULE DEFINITION
*/
static PyModuleDef
operation_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "operation",
    .m_doc = "aio operation c extension.",
    .m_size = -1,
};


/*
    MODULE ENTRYPOINT
*/
PyMODINIT_FUNC
PyInit_operation(void) {

    PyObject* impMod;
    PyObject* impModDict;

    impMod = PyImport_ImportModule("linux_aio.eventfd");
    impModDict = PyModule_GetDict(impMod);

    EventFDClass = PyDict_GetItemString(impModDict, "EventFD");

    impMod = PyImport_ImportModule("linux_aio.context");
    impModDict = PyModule_GetDict(impMod);

    ContextClass = PyDict_GetItemString(impModDict, "AIOContext");

    PyObject *m;

    if (PyType_Ready(&AIOOperationType) < 0) return NULL;

    m = PyModule_Create(&operation_module);

    if (m == NULL) return NULL;

    Py_INCREF(&AIOOperationType);

    if (PyModule_AddObject(m, "AIOOperation", (PyObject *) &AIOOperationType) < 0) {
        Py_DECREF(&AIOOperationType);
        Py_DECREF(m);
        return NULL;
    }

    return m;
}
