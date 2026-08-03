//! Raw io_uring kernel ABI (`linux/io_uring.h`) and syscall wrappers.
//!
//! glibc doesn't wrap these syscalls, so we go through `libc::syscall`
//! directly. Every struct layout and constant here was checked field-for-
//! field against the actual `/usr/include/linux/io_uring.h` on a real Linux
//! box, not copied from memory - a prior attempt at guessing
//! `IORING_SETUP_SINGLE_ISSUER`/`IORING_SETUP_NO_SQARRAY`'s bit positions
//! got them wrong (colliding with `IORING_SETUP_R_DISABLED`/`_TASKRUN_FLAG`).

use std::io;

pub const IORING_OP_FSYNC: u8 = 3;
pub const IORING_OP_ASYNC_CANCEL: u8 = 14;
pub const IORING_OP_READ: u8 = 22;
pub const IORING_OP_WRITE: u8 = 23;

pub const IORING_FSYNC_DATASYNC: u32 = 1 << 0;

pub const IORING_SETUP_SQPOLL: u32 = 1 << 1;
// Unused: pins a ring to a single task in a way this crate's Context API
// doesn't enforce. Kept defined since its bit position was one of the two
// this module verified against a real kernel header.
#[allow(dead_code)]
pub const IORING_SETUP_SINGLE_ISSUER: u32 = 1 << 12;
pub const IORING_SETUP_NO_SQARRAY: u32 = 1 << 16;

pub const IORING_SQ_NEED_WAKEUP: u32 = 1 << 0;

pub const IORING_ENTER_GETEVENTS: u32 = 1 << 0;
pub const IORING_ENTER_SQ_WAKEUP: u32 = 1 << 1;

pub const IORING_FEAT_SINGLE_MMAP: u32 = 1 << 0;

pub const IORING_OFF_SQ_RING: i64 = 0;
pub const IORING_OFF_CQ_RING: i64 = 0x8000000;
pub const IORING_OFF_SQES: i64 = 0x10000000;

pub const IORING_REGISTER_EVENTFD: u32 = 4;
pub const IORING_REGISTER_EVENTFD_ASYNC: u32 = 7;

#[repr(C)]
#[derive(Clone, Copy)]
pub struct IoSqringOffsets {
    pub head: u32,
    pub tail: u32,
    pub ring_mask: u32,
    pub ring_entries: u32,
    pub flags: u32,
    pub dropped: u32,
    pub array: u32,
    pub resv1: u32,
    pub user_addr: u64,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct IoCqringOffsets {
    pub head: u32,
    pub tail: u32,
    pub ring_mask: u32,
    pub ring_entries: u32,
    pub overflow: u32,
    pub cqes: u32,
    pub flags: u32,
    pub resv1: u32,
    pub user_addr: u64,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct IoUringParams {
    pub sq_entries: u32,
    pub cq_entries: u32,
    pub flags: u32,
    pub sq_thread_cpu: u32,
    pub sq_thread_idle: u32,
    pub features: u32,
    pub wq_fd: u32,
    pub resv: [u32; 3],
    pub sq_off: IoSqringOffsets,
    pub cq_off: IoCqringOffsets,
}

impl IoUringParams {
    pub fn zeroed() -> Self {
        // SAFETY: an all-zero io_uring_params is a valid bit pattern (all
        // plain integer fields) and is exactly how the kernel expects it
        // to be initialized before io_uring_setup().
        unsafe { std::mem::zeroed() }
    }
}

/// Field layout matches the kernel's 64-byte `struct io_uring_sqe` exactly
/// for every field this crate touches; the several `union` members the C
/// header declares (e.g. `off`/`addr2`, `rw_flags`/`fsync_flags`) are
/// modeled as single plain fields at the right offset, since we only ever
/// read/write through one interpretation of each union at a time.
#[repr(C)]
#[derive(Clone, Copy)]
pub struct IoUringSqe {
    pub opcode: u8,
    pub flags: u8,
    pub ioprio: u16,
    pub fd: i32,
    pub off: u64,
    pub addr: u64,
    pub len: u32,
    pub op_flags: u32,
    pub user_data: u64,
    pub buf_index: u16,
    pub personality: u16,
    pub splice_fd_in: i32,
    pub addr3: u64,
    pub __pad2: u64,
}

impl IoUringSqe {
    pub fn zeroed() -> Self {
        // SAFETY: an all-zero SQE is a valid, well-defined "do nothing
        // extra" bit pattern for every field this crate sets explicitly
        // afterward.
        unsafe { std::mem::zeroed() }
    }
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct IoUringCqe {
    pub user_data: u64,
    pub res: i32,
    pub flags: u32,
}

const _: () = assert!(std::mem::size_of::<IoUringSqe>() == 64);
const _: () = assert!(std::mem::size_of::<IoUringCqe>() == 16);
const _: () = assert!(std::mem::size_of::<IoUringParams>() == 120);

fn check(result: i64) -> io::Result<i64> {
    if result < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(result)
    }
}

/// Returns the io_uring fd.
pub fn io_uring_setup(entries: u32, params: &mut IoUringParams) -> io::Result<i32> {
    let result = unsafe {
        check(libc::syscall(
            libc::SYS_io_uring_setup,
            entries as libc::c_long,
            params as *mut IoUringParams,
        ))?
    };
    Ok(result as i32)
}

/// # Safety
/// Every in-flight SQE referenced by entries between the ring's current
/// head and `to_submit` must stay valid (its target buffer alive, in
/// particular) until the kernel has consumed it.
pub unsafe fn io_uring_enter(
    fd: i32,
    to_submit: u32,
    min_complete: u32,
    flags: u32,
) -> io::Result<u32> {
    // Always called with a null sigmask, so the sigsz value (8, `_NSIG / 8`)
    // is never dereferenced by the kernel and its exact value is moot.
    let result = check(libc::syscall(
        libc::SYS_io_uring_enter,
        fd as libc::c_long,
        to_submit as libc::c_long,
        min_complete as libc::c_long,
        flags as libc::c_long,
        std::ptr::null::<libc::sigset_t>(),
        8usize,
    ))?;
    Ok(result as u32)
}

pub fn io_uring_register_eventfd(fd: i32, eventfd: i32, async_only: bool) -> io::Result<()> {
    let opcode = if async_only { IORING_REGISTER_EVENTFD_ASYNC } else { IORING_REGISTER_EVENTFD };
    unsafe {
        check(libc::syscall(
            libc::SYS_io_uring_register,
            fd as libc::c_long,
            opcode as libc::c_long,
            &eventfd as *const i32,
            1i64,
        ))?;
    }
    Ok(())
}
