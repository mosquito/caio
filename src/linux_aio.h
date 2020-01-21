#include <sys/eventfd.h>
#include <unistd.h>

#include <stdio.h>
#include <stdlib.h>

#include <linux/aio_abi.h>
#include <sys/syscall.h>

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <structmember.h>


typedef struct {
    PyObject_HEAD
    int fileno;
} EventfdObject;


typedef struct {
    PyObject_HEAD
    aio_context_t ctx;
    unsigned max_requests;
} AIOContext;


typedef struct {
    PyObject_HEAD
    EventfdObject* eventfd;
    AIOContext* context;
    PyObject* py_buffer;
    char* buffer;
    struct iocb iocb;
} AIOOperation;


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
