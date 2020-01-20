#include <sys/eventfd.h>
#include <unistd.h>

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <structmember.h>


typedef struct {
    PyObject_HEAD
    int fileno;
} EventfdObject;


static void
EventfdObject_dealloc(EventfdObject *self) {
    if (self->fileno > 0) close(self->fileno);
    Py_TYPE(self)->tp_free((PyObject *) self);
}

static PyObject *
EventfdObject_new(PyTypeObject *type, PyObject *args, PyObject *kwds) {
    EventfdObject *self;

    self = (EventfdObject *) type->tp_alloc(type, 0);

    if (self != NULL) {
        self->fileno = eventfd(0, 0);

        if (self->fileno <= 0) {
            PyErr_SetFromErrno(PyExc_SystemError);
            return NULL;
        }
    }

    return (PyObject *) self;
}

static int EventfdObject_init(PyObject* self, PyObject *args, PyObject *kwds) {
    static char *kwlist[] = {NULL};
    return PyArg_ParseTupleAndKeywords(args, kwds, "", kwlist);
}

static PyMemberDef EventfdObject_members[] = {
    {"fileno", T_INT, offsetof(EventfdObject, fileno), READONLY,
     "file descritptor"},
    {NULL}  /* Sentinel */
};

static PyTypeObject
EventFDType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "eventfd.EventFD",
    .tp_doc = "linux EventFD descriptor",
    .tp_basicsize = sizeof(EventfdObject),
    .tp_itemsize = 0,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_new = EventfdObject_new,
    .tp_init = (initproc) EventfdObject_init,
    .tp_dealloc = (destructor) EventfdObject_dealloc,
    .tp_members = EventfdObject_members,
};

static PyModuleDef
eventfd_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "eventfd",
    .m_doc = "eventfd c extension.",
    .m_size = -1,
};


PyMODINIT_FUNC
PyInit_eventfd(void) {
    PyObject *m;

    if (PyType_Ready(&EventFDType) < 0) return NULL;

    m = PyModule_Create(&eventfd_module);

    if (m == NULL) return NULL;

    Py_INCREF(&EventFDType);

    if (PyModule_AddObject(m, "EventFD", (PyObject *) &EventFDType) < 0) {
        Py_DECREF(&EventFDType);
        Py_DECREF(m);
        return NULL;
    }

    return m;
}
