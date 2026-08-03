# Анализ надёжности Rust PR и free-threaded Python

Дата анализа: 2026-08-03.

Область анализа: `origin/master...54fabd8` (`rust-implementation`, 50
коммитов), включая Rust/PyO3 backends, общий `caio-core`, pure-Python
fallback, asyncio adapters, сборку и тесты. Появившийся во время анализа
незакоммиченный черновик изменения `src/linux_uring/context.rs` отмечен
отдельно и не считается частью PR HEAD.

## Итог

PR пока не готов к выпуску с заявленной поддержкой free-threaded CPython.
При принятом ограничении «на free-threaded build используются только
`python_aio`/`python_aio_asyncio`, нативные модули имеют другой ABI» это
исправимо без реализации free-threading в Rust backends. Однако эту политику
нужно обеспечить сборкой и import selector, а не полагаться только на то, что
обычный `abi3` wheel случайно не подходит `cp314t`.

Главный текущий blocker находится не в Rust, а в `caio/python_aio.py`: ошибка
пользовательского callback или отсутствие callback убивает единственный
result-handler `multiprocessing.pool.ThreadPool`. После этого последующие
операции выполняют I/O, но их результаты и callbacks больше не доставляются.
Это воспроизводится уже на обычном CPython 3.12 с GIL и видно в штатном
тестовом прогоне.

Краткий приоритет:

| ID | Критичность | Суть |
|---|---:|---|
| PY-01 | Blocker | callback может навсегда сломать result delivery всего Context |
| FT-PY-01 | Blocker | `Context` и `Operation` не имеют согласованного состояния под lock |
| FT-PY-02 | Blocker | один `BytesIO` одновременно является request/result buffer и публичным mutable payload |
| PY-02 | High | capacity неверна даже последовательно и гоняется с completion |
| PY-03 | High | `submit()` гоняется с `close()`, rollback отсутствует |
| FT-ASYNC-01 | High | worker thread обращается к `asyncio.Future` чужого event loop |
| PY-04 | High | asyncio cancellation не отменяет I/O, но позволяет закрыть/reuse fd |
| FT-PACK-01 | High | pure-only policy для free-threaded build явно не обеспечена |
| FT-RUST-01 | Blocker* | PyO3-модули объявляют себя GIL-free-safe, хотя bridge зависит от GIL |
| FT-RUST-02 | Blocker* | Engine/registry/Operation commit не атомарен без GIL |
| RUST-01 | Medium | `linux_uring.process_events()` нарушает `min/max_requests` |
| RUST-02 | Medium | state machine закрытия не подключён к публичному API |
| RUST-03 | Medium | panic/poison превращается в последующие panic через `lock().unwrap()` |
| BUILD-01 | Low | Rust workspace не собирается целиком вне Linux |

\* Blocker только для будущей сборки нативного модуля под free-threaded ABI или
установки из source с таким интерпретатором. Для предложенной pure-only
политики это обязательные рекомендации нативным backends, но не блокер
первого pure-Python релиза.

## Что именно сейчас завязано на GIL

GIL не является корректной заменой application lock: даже в обычном CPython
он может переключить поток между bytecode instructions, а blocking I/O
освобождает GIL. Тем не менее текущий код использует его как неявный барьер в
следующих местах.

| Место | Что фактически даёт GIL сейчас | Что меняется без GIL |
|---|---|---|
| `python_aio.py:53-69`, `_in_progress` | отдельные чтения/записи Python `int` не исполняются параллельно | `+=`/`-=` становятся read-modify-write гонками; возможны lost update, отрицательный/зависший счётчик и переполнение capacity |
| `python_aio.py:96-108`, `Operation.buffer` | `BytesIO.write/getvalue` и публичный доступ не выполняются одновременно на двух CPU | worker, `get_value()`, `payload` и повторный submit могут одновременно читать/менять один buffer |
| `python_aio.py:53-62, 236-249`, result fields | типичный asyncio-путь видит `exception/written/buffer` после callback | другой поток может увидеть смесь старого и нового результата; единого terminal commit нет |
| `python_aio.py:267-269`, callback | присваивание и вызов callback сериализованы с другим Python-кодом | `set_callback()` гоняется с completion; не определено, какой callback будет вызван |
| `python_aio.py:85-94`, `_locks[fd]` fallback | создание lock в `defaultdict` практически сериализуется | нельзя полагаться на внутренние locks `dict/defaultdict` как на атомарную операцию «найти или создать один lock для fd» |
| `asyncio_base.py:66-77` | чтение `future.done()` не параллельно event-loop mutation | result thread читает объект `Future`, принадлежащий другому loop thread |
| `src/*/context.rs`, submit | Python thread удерживает GIL между Engine submit и Python registry insert; worker `Python::attach()` ждёт | worker/другой Python thread может забрать completion до registry insert и потерять его |
| `src/*/operation.rs`, lifecycle/result | другой Python call не может войти между отдельными Rust locks/atomics, пока bridge держит GIL | concurrent submit одного Operation, ранний `in_flight=false` и частично опубликованный result становятся наблюдаемыми |

Free-threaded CPython ставит внутренние locks на часть built-in containers, но
это обеспечивает memory safety отдельных операций, а не атомарность
протокола из нескольких действий. Официальная документация прямо рекомендует
использовать `threading.Lock`, а не полагаться на эти внутренние locks:
[Python support for free threading](https://docs.python.org/3.14/howto/free-threading-python.html#thread-safety).

## Pure-Python backend: найденные проблемы

### PY-01 — callback ломает весь ThreadPool result path

Критичность: **Blocker**, воспроизводится с GIL.

`Context._execute()` без проверки вызывает `operation.callback(...)` из
`on_success`/`on_error` (`caio/python_aio.py:53-62`). Эти функции сами
являются callback/error_callback объекта `ApplyResult`.

`multiprocessing.pool.Pool._handle_results()` не изолирует исключение,
вылетевшее из `ApplyResult._set()`. Поэтому:

1. raw API позволяет submit без `set_callback()`, значение по умолчанию —
   `None`;
2. `operation.callback(...)` поднимает `TypeError`, либо пользовательский
   callback поднимает своё исключение;
3. единственный `_handle_results` thread завершается;
4. все следующие результаты остаются в cache, `_in_progress` не уменьшается,
   callbacks/futures никогда не завершаются;
5. финализатор Pool затем поднимает
   `AssertionError: Cannot have cache with result_handler not alive`.

Это уже видно в `uv run pytest -q`: тесты формально зелёные, но pytest
сообщает пять `PytestUnhandledThreadExceptionWarning` из
`python_aio.py:57`, а завершение процесса печатает ошибки Pool finalizer.
Отдельный regression показал: после callback, поднявшего `RuntimeError`,
callback следующей операции не вызывается и `_in_progress` остаётся равным 1.

Исправление:

- отсутствие callback должно быть нормальным случаем;
- пользовательский callback вызывать вне внутренних locks и оборачивать в
  `try/except BaseException`, направляя ошибку в `sys.unraisablehook` или
  loop exception handler;
- инфраструктурный result thread никогда не должен видеть исключение
  callback;
- предпочтительно уйти с внутреннего API
  `multiprocessing.pool.ThreadPool` на `concurrent.futures.ThreadPoolExecutor`
  либо небольшой явно управляемый pool. При выборе Executor отдельно решить
  shutdown: его non-daemon workers могут задержать завершение интерпретатора
  на застрявшем syscall;
- включить
  `error::pytest.PytestUnhandledThreadExceptionWarning` в pytest/CI.

### FT-PY-01 — отсутствует state machine Operation

Критичность: **Blocker** для free-threading, **High** для raw API с GIL.

У `Operation` независимые mutable поля `callback`, `buffer`, `exception`,
`written`, но нет lock и состояния `NEW/IN_FLIGHT/DONE`. Pure backend
намеренно разрешает повторно submit-ить тот же объект
(`tests/test_raw_low_level.py`, test `resubmission_behavior_is_backend_specific`).

Два concurrent submit одного объекта приводят к двум I/O, пишущим в один
result object. Completion одного запроса может:

- перезаписать result/exception другого;
- вызвать callback, установленный уже для другого поколения;
- вернуть `get_value()` между несколькими несогласованными обновлениями;
- для read записать оба результата в один `BytesIO` с общей позицией.

Рекомендация — унифицировать pure backend с новыми native backends и сделать
Operation one-shot:

```text
NEW -> CLAIMED -> IN_FLIGHT -> SUCCEEDED
                            ├-> FAILED
                            └-> CANCELLED
```

Все lifecycle/result/callback поля должны быть одним объектом состояния под
`threading.Lock`. `submit()` атомарно делает `NEW -> CLAIMED`; ошибка
подготовки или scheduling выполняет rollback в `NEW`; terminal completion
сначала полностью записывает result/exception, затем меняет state и только
после освобождения lock вызывает callback.

Если обратная совместимость требует resubmit, нужен отдельный generation id и
отдельный result на поколение. Повторное использование одного mutable
Operation без generation не может быть надёжным.

### FT-PY-02 — `BytesIO` одновременно request, result и public buffer

Критичность: **Blocker** для free-threading, **High** и без него.

Для write `Operation.buffer` хранит пользовательские данные, worker вызывает
`getvalue()`. Для read worker пишет результат прямо в тот же `BytesIO`.
Свойство `payload` возвращает writable live `memoryview`
(`python_aio.py:260-261`).

Проблемы:

- на free-threaded build public thread может читать/менять view одновременно
  с worker;
- один read Operation, submit-нутый дважды, имеет один cursor и объединяет
  результаты;
- caller может изменить write payload уже после создания/submit;
- если caller удерживает `view = op.payload` для пустого read buffer,
  последующий `BytesIO.write()` может завершиться `BufferError`, потому что
  exported buffer нельзя resize;
- внутренний lock `BytesIO`, даже если он есть в конкретной версии CPython,
  не делает последовательность операций caio линейризуемой.

Исправление: не делиться mutable buffer.

- write: сохранить immutable `bytes` snapshot при создании Operation;
- worker получает только immutable request fields;
- read handler возвращает новый `bytes`, не меняя Operation;
- completion под state lock присваивает terminal immutable `bytes`;
- `payload`/`get_value` берут согласованный snapshot под lock;
- во время `IN_FLIGHT` возвращать явный `RuntimeError`, как уже делают native
  backends.

### PY-02 — capacity check неверен и не атомарен

Критичность: **High**.

`if self._in_progress > self.__max_requests` (`python_aio.py:64`) — ошибка на
единицу. При `max_requests=1` последовательно принимаются две операции.

Кроме того, check, increment, `apply_async` и decrement — четыре независимых
действия. Без GIL concurrent `+=/-=` теряют updates. Даже с GIL два submit
могут пройти check до increment. Счётчик освобождается до полной публикации
`written` и callback (`python_aio.py:59-62`).

Исправление:

- валидировать `max_requests > 0`;
- reserve capacity и `OPEN` check выполнять одной секцией под Context lock;
- условие — `in_progress >= max_requests`;
- ровно один `finally`-путь освобождает slot после terminal commit;
- scheduling failure обязательно возвращает slot;
- тестировать invariant `0 <= in_progress <= max_requests` через barrier,
  а не только вероятностным stress.

`threading.BoundedSemaphore` тоже подходит, но Context всё равно нужен один
lock для согласования slot, close и registry операций.

### PY-03 — `submit()`/`close()` и rollback

Критичность: **High**.

`close()` проверяет `_closed` вне `_closed_lock`, не перепроверяет внутри и
не синхронизирован с `_execute()`. Возможный порядок:

1. submit увеличил `_in_progress`;
2. другой thread выполнил `pool.close()`;
3. `apply_async()` поднял `ValueError("Pool not running")`;
4. `_in_progress` навсегда завышен, Operation остался в неопределённом
   состоянии.

Два concurrent `close()` также оба могут войти в `pool.close()`.
`__del__` написан как `if self.pool.close(): self.close()`: `close()` у Pool
возвращает `None`, поэтому метод Context не вызывается и `_closed` не
обновляется; во время interpreter finalization это уже приводит к ignored
exceptions.

Исправление:

- состояния Context: `OPEN -> CLOSING -> CLOSED` под одним lock;
- под этим же lock запретить новый schedule после `CLOSING`;
- scheduling и публикация операции должны иметь rollback;
- `close()` idempotent, с повторной проверкой внутри lock;
- `__del__` только best-effort вызывает уже безопасный `close()` и подавляет
  только ожидаемые finalization errors;
- предоставить `wait_closed()/aclose()` с документированным deadline.

### FT-ASYNC-01 — доступ к Future из result thread

Критичность: **High** для free-threading.

`AsyncioContextBase._on_done()` вызывается из pool result thread и сначала
делает `future.done()` (`caio/asyncio_base.py:72`). Future принадлежит event
loop thread. Официальный asyncio contract требует использовать
`loop.call_soon_threadsafe()` при обращении из другого OS thread:
[Developing with asyncio](https://docs.python.org/3.14/library/asyncio-dev.html#concurrency-and-multithreading).

Проверка `done()` до scheduling не нужна: lambda уже повторяет её в loop
thread. Worker path должен только вызвать:

```python
self.loop.call_soon_threadsafe(self._finish_future, future)
```

а `_finish_future()` проверяет/меняет Future уже в его loop. Нужно обработать
`RuntimeError`, если loop успели закрыть, не пропуская исключение в
ThreadPool result handler.

Также следует явно зафиксировать: один `AsyncioContext` принадлежит одному
event loop и не разделяется между loop threads. На free-threaded Python
рекомендуется отдельный event loop и Context на thread.

### PY-04 — asyncio cancellation не означает отмену I/O

Критичность: **High** для жизненного цикла fd, не специфично GIL.

При `CancelledError` adapter вызывает `context.cancel(op)`, но pure backend
всегда возвращает 0. Coroutine немедленно завершается, semaphore slot
освобождается, а syscall может ещё не начаться. Caller естественно может
выйти из `with open(...)`, закрыть fd, после чего queued operation использует
закрытый либо уже переиспользованный номер fd.

В `abstract.py` есть fd lifetime contract, но adapter cancellation визуально
выглядит как завершённая отмена. Нужно одно из:

- при cancel Task ждать реального terminal completion перед окончательным
  освобождением operation/fd responsibility;
- дублировать fd на submit и закрывать duplicate на completion;
- либо явно документировать, что отмена coroutine только abandons waiter,
  I/O продолжается, а fd нельзя закрывать до `wait_closed()`.

`close()`/`__aexit__()` также должны либо drain-ить, либо возвращать awaitable
`aclose()`; текущий `pool.close()` только запрещает новые задачи.

### PY-05 — валидация границ

Критичность: **Medium**.

- `assert pool_size < MAX_POOL_SIZE` исчезает с `python -O`; это не
  пользовательская валидация;
- нет явной проверки `pool_size > 0`, `max_requests > 0`, integer/bool
  semantics;
- negative `nbytes`, fd, offset проходят далеко в worker и становятся
  асинхронной ошибкой;
- `set_callback()` не проверяет callable;
- callback type говорит `Callable[[int], Any]`, но error path передаёт
  `None`.

Проверки должны происходить синхронно до claim/schedule и совпадать между
backends настолько, насколько позволяет OS API.

## Как сделать pure-Python реализацию пригодной для free-threading

Рекомендуемая минимальная архитектура:

1. `Operation` хранит immutable request (`opcode`, `fd`, `offset`,
   `write_payload: bytes`, requested size) и один `_state_lock`.
2. Под `_state_lock` лежат lifecycle, callback, result bytes/int и exception.
3. `Context` имеет `_state_lock`, `OPEN/CLOSING/CLOSED`, `in_progress` и набор
   in-flight operations.
4. `submit()` сначала валидирует весь batch, затем claim-ит operations и
   резервирует slots. Scheduling failure откатывает оба ресурса.
5. Worker не меняет Operation: он только возвращает `bytes/int` или exception.
6. Completion атомарно публикует terminal state, удаляет Operation из
   Context registry и освобождает slot.
7. Callback вызывается последним, без внутренних locks, и его исключение
   никогда не выходит в pool infrastructure.
8. Asyncio callback из worker только ставит событие через
   `call_soon_threadsafe`; Future трогает только loop thread.
9. `close/aclose` запрещает submit, задаёт явную policy для queued/running
   I/O и имеет bounded wait.
10. Для платформ без `pread/pwrite` получение per-fd lock выполняется под
    отдельным registry lock; сам `lseek+read/write` остаётся под per-fd lock.

Грубый lock order, который стоит записать рядом с кодом:

```text
Context state lock -> Operation state lock
```

User callback и blocking syscall не выполняются ни под одним из этих locks.
Нельзя допустить обратный порядок в completion/cancel/close.

## Как гарантировать pure-only policy на free-threaded Python

Обычный `cp310-abi3` wheel действительно не должен загружаться на CPython
3.14t: free-threaded 3.14 требует version-specific `cp314t`; с 3.15 есть
отдельный `abi3t`. См.
[PyO3 build/distribution guide](https://pyo3.rs/v0.29.0/building-and-distribution#py_limited_apiabi3abi3t)
и
[CPython 3.15 abi3t migration](https://docs.python.org/3.15/howto/abi3t-migration.html).

Но одного ABI mismatch недостаточно:

- если compatible wheel отсутствует, pip может собрать sdist;
- PyO3 при free-threaded interpreter способен собрать version-specific
  native extension;
- пользователь может собрать модуль напрямую;
- на 3.15 будущий `abi3t` artifact уже будет совместим.

Поэтому pure-only policy должна быть явной.

1. В `caio/__init__.py` проверить build configuration:

   ```python
   FREE_THREADED_BUILD = bool(
       sysconfig.get_config_var("Py_GIL_DISABLED")
   )
   ```

   Это лучше `sys._is_gil_enabled()`: решение относится к ABI/build, а
   runtime GIL можно временно включить через `PYTHON_GIL=1`.

2. На free-threaded build не пытаться импортировать native backends, выставить
   их в `None`, а `variants` сформировать только из `python_aio`.
3. Не разрешать `CAIO_IMPL=thread/linux/uring` обходить этот guard.
4. Публиковать pure `py3-none-any` wheel, который free-threaded pip сможет
   выбрать, пока platform `abi3` wheels обслуживают обычный CPython.
5. PEP 517 backend при `Py_GIL_DISABLED=1` должен либо строить pure wheel,
   либо завершаться с ясной ошибкой, но не молча собирать неаудированный
   native module.
6. Проверить wheel selection в чистом CPython 3.14t/3.15t окружении, включая
   установку без binary cache.

Текущий `scripts/build_backend.py` всегда делегирует primary extension
maturin и жёстко называет Linux siblings `<name>.abi3.so`. Для source build
под 3.14t это неверно: version-specific extension должна иметь `cpython-314t`
suffix. Поэтому текущая сборка из sdist не является безопасным fallback для
free-threaded пользователя.

## Рекомендации нативным Rust/PyO3 backends

Они не нужны для первого pure-only free-threaded релиза, но нужны до любой
будущей публикации `cp314t`/`abi3t`.

### FT-RUST-01 — модули объявлены GIL-free-safe

Критичность: **Blocker** для native free-threaded build.

Все три `lib.rs` используют обычный `#[pymodule]`. Начиная с PyO3 0.28,
default означает attestation `Py_MOD_GIL_NOT_USED`. В PyO3 0.29
`Python::attach()` на free-threaded build только attach-ит thread к runtime и
не сериализует его с другими threads:
[PyO3 free-threading guide](https://pyo3.rs/v0.29.0/free-threading.html).

До исправления следующего раздела следует явно поставить:

```rust
#[pymodule(gil_used = true)]
```

на всех модулях. Это защитит direct/source builds: CPython включит GIL при
импорте и выдаст warning. После полного аудита можно явно перейти на
`gil_used = false`.

### FT-RUST-02 — submit и result publication не атомарны

Критичность: **Blocker** для native free-threaded build.

Во всех Context схема одна:

1. `already_submitted()` под Operation mutex;
2. fallible `build_spec()`;
3. `engine.lock().submit_many()`;
4. engine lock отпускается;
5. `mark_submitted()` и insert под отдельным registry lock.

Примеры: `src/thread_aio/context.rs:143-188`,
`src/linux_uring/context.rs:118-154`, аналогично `linux_aio`.

Без GIL:

- два threads одновременно видят один Operation как NEW и submit-ят его в
  один или разные Context;
- fast worker может завершить request и `wake()` может poll/remove completion
  до registry insert;
- `filter_map` в `wake/deliver` молча отбрасывает completion без registry
  entry;
- callback/future теряется навсегда, а поздно вставленный registry entry
  течёт.

Отдельно `mark_submitted()` пишет `request_id`, затем `in_flight=true`, а
`apply_result()` сначала пишет `in_flight=false`, затем result buffer/result/
error (`src/thread_aio/operation.rs:153-197`; тот же pattern в двух Linux
operations). Даже `SeqCst` не исправляет неверный логический порядок.

Рекомендуемый протокол:

1. Operation CAS/mutex: `NEW -> CLAIMED` до build/Engine.
2. На любой ошибке: `CLAIMED -> NEW`.
3. Объединить Engine и Python registry в один `ContextState` под одним mutex
   либо держать оба lock в строгом порядке до окончания commit.
4. Accepted Operation и registry entry публиковать до освобождения Context
   lock; worker `wake()` ждёт этот lock.
5. Rejected operations атомарно rollback-ить.
6. Все result/error/buffer/lifecycle поля Operation держать в одном state
   mutex; terminal state публиковать последним.
7. Missing registry entry для известного Engine completion считать internal
   error, а не молча отбрасывать через `filter_map`.
8. User callbacks выполнять после освобождения всех locks.

### RUST-01 — `linux_uring.process_events()` не соблюдает bounds

Критичность: **Medium**.

В wait loop `cq_available()` сравнивается с `min_requests`
(`src/linux_uring/context.rs:214-237`). CQ может содержать служебный
`ASYNC_CANCEL` sentinel, который driver затем не возвращает как terminal
Operation completion. Поэтому `min_requests=1` способен завершить ожидание,
не доставив ни одного пользовательского результата.

После `poll()` список делится по `max_requests`, но обе части немедленно
передаются в `deliver()` (`context.rs:240-250`). Метод возвращает 1, но может
синхронно вызвать callbacks для всех N результатов. Это нарушает
backpressure и observable contract. Отрицательный timeout здесь молча
становится нулём, тогда как `linux_aio` уже возвращает `ValueError`.

Нужна bridge-side pending completion queue. `min_requests` считать после
фильтрации служебных CQE, а за один call доставлять не больше
`max_requests`. Timeout validation унифицировать.

В рабочем дереве уже появился незакоммиченный черновик с
`pending: Mutex<VecDeque<...>>` и bounded drain. По замыслу он закрывает две
основные части finding, но ещё не входит в `54fabd8`; для него нужны
regression tests на FIFO, cancel sentinel, reentrant callback и concurrent
`flush/process_events`. Отрицательный timeout этим черновиком пока не
отклоняется.

### RUST-02 — close state machine не подключён к Python API

Критичность: **Medium**.

`caio-core::Engine` реализует `begin_close()/finish_close()`, но PyO3 Context
не предоставляет `close/aclose` и bridge их не вызывает.
`AsyncioContextBase.close()` удаляет event-loop reader либо закрывает только
pure Pool, но native Context остаётся usable.

Нужны idempotent native `close()` и bounded `aclose()`:

- запрет submit после `CLOSING`;
- queued requests завершить согласованной ошибкой;
- in-flight drain/cancel с deadline;
- callbacks не вызывать из `Drop`;
- asyncio wrapper после close не должен оставлять рабочий `self.context`.

### RUST-03 — panic и poisoned mutex

Критичность: **Medium**.

Python-facing код массово использует `lock().unwrap()`. `Engine::poll()`
намеренно panic-ует при duplicate/unknown driver event. Если panic произошёл
под mutex, он становится poisoned, и все дальнейшие Python calls снова
panic-уют на `unwrap()`.

Internal invariant violation допустимо считать bug, но extension не должен
создавать каскад panic внутри долгоживущего Python процесса. На boundary:

- panic ловить/преобразовывать в стабильный `PyRuntimeError` там, где это
  безопасно;
- poison обрабатывать явно (`into_inner` для controlled shutdown либо
  permanent failed Context state);
- fuzz/property tests гоняют duplicate, stale, cancel и shutdown events;
- не удерживать locks при user callback — это уже в основном соблюдено.

### BUILD-01 — workspace вне Linux

Критичность: **Low** для runtime, **Medium** для сопровождения.

Корневой workspace безусловно включает Linux-only crates. На macOS
`cargo test --workspace` не компилируется из-за отсутствующих
`SYS_io_*`, `SYS_io_uring_*`, `eventfd`, `MAP_POPULATE` и различий
`c_long`. CI запускает workspace clippy/tests только на Ubuntu; Windows и
macOS проверяют Python layer, но не полный Rust workspace.

Нужно либо cfg-gate Linux crates, либо формировать platform-specific Rust
test matrix. `caio-core`, `caio-backend-thread` и `caio-thread-aio` должны
проверяться Rust CI на Windows/macOS отдельно.

## Что в PR уже исправлено

Следующие проблемы из ранней версии ветки закрыты текущими коммитами и не
считаются открытыми findings:

- caller-controlled infallible Rust allocations заменены fallible paths
  (`92035fa`);
- отрицательный `linux_aio` timeout отклоняется (`4dad6c4`);
- ожидание thread workers в Drop ограничено deadline (`f2fee13`);
- завершённая `linux_aio` Operation очищает Context back-reference
  (`acad4c5`);
- abandoned `linux_aio` Context/Operation cycle теперь виден Python GC
  (`54fabd8`);
- ряд transactional submit, EINTR, cross-context cancel и ring-space ошибок
  покрыт отдельными fix commits и regression tests.

Это заметно улучшает обычный GIL build, но не устраняет описанные выше
bridge-level free-threading транзакции и проблемы pure backend.

## Обязательные тесты

### Pure-Python, обычный CPython

- submit без callback завершается и не ломает Context;
- callback поднимает `Exception`, `SystemExit` и `KeyboardInterrupt`;
  последующие операции всё равно завершаются;
- callback reentrant вызывает `submit/close/get_value`;
- `payload` удерживается до/во время read submit;
- `max_requests=1` никогда не имеет больше одного in-flight request;
- `submit` гоняется с `close`, scheduling failure не оставляет slot/claim;
- `__del__` не печатает ignored exceptions;
- CI падает на любом `PytestUnhandledThreadExceptionWarning`.

### CPython 3.14t и 3.15t, `PYTHON_GIL=0`

- assert, что `caio.variants == (caio.python_aio,)` и native import не
  предпринимался;
- 10–100 threads одновременно submit-ят разные operations;
- два threads submit-ят один Operation;
- barriers гоняют submit/completion/get_value/payload/set_callback;
- счётчик и Context state invariants проверяются после каждого раунда;
- close/GC/cancel гоняются с blocked worker;
- один event loop + Context на thread; cross-thread Future access запрещён;
- wheel install тестируется из binary artifacts и из sdist/no-binary mode.

### Нативные backends, до будущего `cp314t/abi3t`

- concurrent claim одного Operation в одном и разных Context;
- completion между Engine dispatch и registry commit;
- concurrent poll/flush/cancel/close;
- result readers во время terminal publication;
- callbacks reentrant и concurrent;
- `min/max_requests` с cancel sentinel и pending queue;
- Rust state tests через `loom` либо детерминированный scheduler.

## Выполненная проверка

На macOS, CPython 3.12 с GIL:

- `uv run pytest -q`: **52 passed, 39 skipped**, но пять
  `PytestUnhandledThreadExceptionWarning` из pure backend и несколько ошибок
  Pool/Context finalizers после summary; формально нулевой exit code не
  означает чистый прогон;
- отдельный callback regression подтвердил потерю всех следующих results
  после одного исключения callback;
- `cargo test -p caio-core -p caio-backend-thread -p caio-thread-aio`:
  **42 Rust tests passed**;
- `cargo test --workspace`: ожидаемо не компилируется на macOS из-за
  Linux-only crates;
- free-threaded interpreter в окружении отсутствует, поэтому FT races
  подтверждены анализом lock/publication порядка, но динамически здесь не
  исполнялись.

## Рекомендуемый порядок работ

1. Исправить PY-01 и сделать thread warnings ошибками CI.
2. Ввести единые state locks/lifecycle в pure Context и Operation, убрать
   shared mutable `BytesIO`.
3. Исправить capacity, submit/close rollback и asyncio thread boundary.
4. Определить cancellation/fd/close contract и реализовать `aclose`.
5. Явно обеспечить pure-only free-threaded packaging/import policy.
6. Добавить 3.14t/3.15t `PYTHON_GIL=0` CI.
7. Для native source-build safety сейчас поставить `gil_used = true`.
8. До любых native `cp314t/abi3t` artifacts исправить atomic bridge commit,
   result publication и `linux_uring.process_events`.
