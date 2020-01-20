#include <linux/aio_abi.h>

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <structmember.h>


#include <sys/syscall.h>
#include <linux/aio_abi.h>

inline int io_setup(unsigned nr, aio_context_t *ctxp) {
	return syscall(__NR_io_setup, nr, ctxp);
}

inline int io_destroy(aio_context_t ctx) {
	return syscall(__NR_io_destroy, ctx);
}

inline int io_submit(aio_context_t ctx, long nr, struct iocb **iocbpp) {
	return syscall(__NR_io_submit, ctx, nr, iocbpp);
}

inline int io_getevents(aio_context_t ctx, long min_nr, long max_nr,
		struct io_event *events, struct timespec *timeout) {
	return syscall(__NR_io_getevents, ctx, min_nr, max_nr, events, timeout);
}


typedef struct {
    PyObject_HEAD
    aio_context_t ctx;
} AIOContext;


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
    size_t *max_requests = 0;
    self->ctx = 0;

    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|I", kwlist, &max_requests)) {
        return -1;
    }

    if (max_requests == 0) {
        return -1;
    }

    if (io_setup(max_requests, &(self->ctx)) < 0) {
        PyErr_SetFromErrno(PyExc_SystemError);
        return -1;
    }

    return 0;
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
