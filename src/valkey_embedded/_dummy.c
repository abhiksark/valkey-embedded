/* src/valkey_embedded/_dummy.c — empty C extension so the wheel is platform-tagged. */
#include <Python.h>

static struct PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT, "_dummy", NULL, -1, NULL,
};

PyMODINIT_FUNC PyInit__dummy(void) {
    return PyModule_Create(&moduledef);
}
