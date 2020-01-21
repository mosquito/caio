#include "linux_aio.h"


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
        "value",
        T_INT,
        offsetof(AIOContext, ctx),
        READONLY,
        "context value descriptor"
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

/*
    AIOContext class
*/
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
    .tp_repr = (reprfunc) AIOContext_repr
};

/*
    MODULE DEFINITION
*/
static PyModuleDef
context_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "context",
    .m_doc = "aio context binding",
    .m_size = -1,
};


/*
    MODULE ENTRYPOINT
*/
PyMODINIT_FUNC
PyInit_context(void) {
    PyObject *m;

    if (PyType_Ready(&AIOContextType) < 0) return NULL;

    m = PyModule_Create(&context_module);

    if (m == NULL) return NULL;

    Py_INCREF(&AIOContextType);

    if (PyModule_AddObject(m, "AIOContext", (PyObject *) &AIOContextType) < 0) {
        Py_DECREF(&AIOContextType);
        Py_DECREF(m);
        return NULL;
    }

    return m;
}
