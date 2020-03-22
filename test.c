#include <pthread.h>

pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
pthread_cond_t cond = PTHREAD_COND_INITIALIZER;


void* inner() {
    while(1) {
        pthread_mutex_lock(&lock);

        while(is_empty_queue())
            pthread_cond_wait(&cond, &lock);

        buffer = get_queue();
        pthread_mutex_unlock(&lock);
        /* декодируем данные в буфере */

        /* посылаем декодированные данные на выход для воспроизведения */


      }
}
