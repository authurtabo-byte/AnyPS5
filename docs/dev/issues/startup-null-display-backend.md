# Startup crash: display interface built with a null backend pointer

Status: open, not fixed. Root cause localized; fix blocked on guest-side job
ordering / display-service population.

## Summary

A title crashes during startup on its graphics thread. The display constructor
builds a display wrapper from a smart pointer to the display backend that is
entirely zero (null data, control and ref fields), then dereferences it and
makes a virtual call through a null vtable. The title never reaches asset
loading, middleware-library parsing, AGC, or Vulkan device creation before the
crash.

## Crash

- Thread: graphics thread
- `rip`: `app.exe+0x51fa84`
- Fault: read of `0x0` (`mov rax,[rdi]` with `rdi = 0`)
- Guest heap addresses are deterministic across runs, so the crash is
  reproducible and analyzable.
- Reproduces identically on both host GPUs (integrated and discrete), i.e. it is
  independent of Vulkan device choice — it happens before any Vulkan device is
  created.

## Code path

1. Display constructor at `0x524e80` (slot 0 of vtable `0x902eb0`) is invoked on
   the parent display object (`0x200441800` at crash time).
2. It reads `rsi = this+8` (a smart pointer to the display backend),
   `rdi = *(this+0x48)`, `rdx = *(*(singleton 0x942a90)+0x198)`, then
   `call 0x51fa00`.
3. Wrapper builder `0x51fa00`: `operator new(0x70)`, sets vtable `0x910420`,
   copies source fields, then:
   ```
   0x51fa7c: mov rdi,[rbx+0x10]   ; rbx = source = parent+8; rdi = source.ptr
   0x51fa80: mov [r15+0x40],rdi
   0x51fa84: mov rax,[rdi]        ; CRASH: rdi = 0
   0x51fa8e: call [rax]           ; virtual call on null interface
   ```
   The wrapper **unconditionally** dereferences `source[+0x10]` and virtual-calls
   through it. There is no null guard.

## Object state at crash (guest heap, from `APS5_FRAME_REGDUMP` / `APS5_DUMP_ADDR`)

Parent display object `0x200441800` (vtable `0x902eb0` = display interface):

```
+0x00: 0x902eb0   (vtable: display interface)
+0x08: 0          <-- backend smart pointer .ptr  = NULL
+0x10: 0          <-- .ctrl = NULL
+0x18: 0          <-- .ref  = NULL
+0x20: 1
+0x28: 0x20088fe10
+0x48: 0x200841738   (this+0x48, read by the display constructor)
+0x58: 0x2004415f0
+0x68: 0x200441800   (self backref)
```

The parent's backend smart pointer at `parent+8` is **entirely zero**: the
backend was never assigned. A watchpoint on the field caught **zero writes** — it
is left at its zero-initialized value.

## Producer of the source struct

`0x4c0340` is a generic smart-pointer copy helper (`{ptr, ctrl, ref}`):

- Non-empty path: copies `src+0x28 -> +0`, `src+0x30 -> +8`, `src+0x38 -> +0x10`.
- Empty path (`0x4c03de`): zeroes all three fields when the upstream virtual call
  at `0x4c0393` (`mov (%rsi),%rax; call *0x18(%rax)`) returns a null inner
  pointer.

## Job-dispatch mechanism (frame #1 = `0x4a9b10`)

`0x4a9b10` is a job-queue drain on the manager object (`rbx = 0x2004211d0`,
vtable `0x904a88`):

```
0x4a9b4c: cmp byte [rbx+0xa0], 1   ; "running" gate
0x4a9b53: jne 0x4a9cd3             ; if != 1, skip all jobs and return
0x4a9b62: mov rcx,[rbx+0x60]       ; job list head
0x4a9bb7: call qword [rax]         ; run job slot 0 -> display ctor -> CRASH
```

Vtable `0x904a88` slot 2 = `0x4ce030` (the factory host that calls
`[this+0x60]+0x20`). The display is produced by a **job**, and the backend is
supposed to be stored into `parent+8` by an earlier job/service that did not run
(or returned null).

## RTTI (decoded from the exe string table)

```
ServiceCompound<
  ServiceCompound< ServiceCompound<
    DummyService, JobSync::CompleteFunctor*>,
    StoreData< SharedPtr<DisplayInterface,DisplayBackend> >*>,
  QuickSingletonService< Singleton<IGraphicsService>::Pointer<GraphicsServiceBase> >,
  NullType>::ServiceCommand< CreatePrimary, ... >
```

The display smart pointer is produced by an **async `JobSync` completion** stored
via `StoreData`, gated on the `IGraphicsService` singleton. The graphics stack
also references a middleware FX library parser and a graphics asset cache.

## Evidence that this is not a missing SCE export

- All SCE imports resolve (with the correct library set for this title).
- VideoOut is exercised and succeeds before the crash:
  ```
  sceVideoOutOpen(userId=255, busType=0) -> handle 1
  sceVideoOutGetOutputStatus(1)
  sceVideoOutIsOutputSupported(1, mode 0xd000000a)
  sceVideoOutIsOutputSupported(1, mode 0x1)
  sceVideoOutSetBufferAttribute2(0x8100000000000000, tiling 0, 3840x2160)
  sceVideoOutRegisterBuffers2(1, set 0, start 0, num 2) -> OK
  sceVideoOutSetFlipRate(1, 0) -> 0
  sceVideoOutAddFlipEvent(eq=1, handle=1) -> 0
  <CRASH in guest, no further SCE entry>
  ```
- No AGC call happens (`agc_trace.log` never created).
- No asset or middleware library file is ever opened.
- The null backend is produced entirely in guest code with no additional SCE
  entry point.

## Conclusion

The parent display object holds a smart pointer to the display backend that is
null. The backend was never constructed because its upstream graphics/display
service (singleton `0x942a90`, service at `+0xb8`) did not produce a backend, and
the async job that should populate `parent+8` did not run before the graphics
thread ran the constructor.

This is a **guest-side service/job ordering or graphics-service capability gap**,
not a missing SCE call. AnyPS5 does not currently expose guest single-stepping or
call hooks, so the remaining unknown — *why the backend-producing job did not
run* — cannot be traced from the host side.

## Next steps (require guest instrumentation on real hardware or new tooling)

- Instrument the factory at `this+0x60` (`call [rax+0x20]` @ `0x4ce050`) and the
  service `[0x942a90]+0x198` to identify which method returns the null interface.
- Identify the concrete class of the object at `parent+0x60` (its vtable) and its
  `+0x20` method; that method should return a live backend.
- Determine whether the graphics service / base is expected to be created eagerly
  at this startup point, or whether the `JobSync` completion that stores the
  display smart pointer is being skipped due to the `[manager+0xa0]` running
  gate.

## Reproduction

1. Relink the title's `decrypted/eboot.bin` with the correct library set:
   `relinker.exe --windows decrypted/eboot.bin app.exe`
2. Lay out `app.exe`, `libs/*.prx`, and `app0/` together.
3. Launch `app.exe`. It crashes on the graphics thread at `+0x51fa84`.

## Instrumentation used (uncommitted, candidate for a PR)

- `CrashReport.cpp`: `APS5_FRAME_REGDUMP` (dump saved registers / pointed-to
  objects per frame), `APS5_DUMP_ADDR` (dump an arbitrary guest address),
  `APS5_WATCH_ADDR` (watch writes to a guest address).
- `libSceVideoOut` / `libSceAgcDriver`: per-entry tracing of the VideoOut and AGC
  driver call sequence.
