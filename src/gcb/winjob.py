"""Start a Windows child suspended, bind it to a kill-on-close Job, then run it."""
import ctypes
from ctypes import wintypes
import subprocess


CREATE_SUSPENDED = 0x00000004
TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit",ctypes.c_int64),
                ("PerJobUserTimeLimit",ctypes.c_int64),
                ("LimitFlags",wintypes.DWORD),
                ("MinimumWorkingSetSize",ctypes.c_size_t),
                ("MaximumWorkingSetSize",ctypes.c_size_t),
                ("ActiveProcessLimit",wintypes.DWORD),
                ("Affinity",ctypes.c_size_t),
                ("PriorityClass",wintypes.DWORD),
                ("SchedulingClass",wintypes.DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount",ctypes.c_uint64),
                ("WriteOperationCount",ctypes.c_uint64),
                ("OtherOperationCount",ctypes.c_uint64),
                ("ReadTransferCount",ctypes.c_uint64),
                ("WriteTransferCount",ctypes.c_uint64),
                ("OtherTransferCount",ctypes.c_uint64)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation",JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo",IO_COUNTERS),
                ("ProcessMemoryLimit",ctypes.c_size_t),
                ("JobMemoryLimit",ctypes.c_size_t),
                ("PeakProcessMemoryUsed",ctypes.c_size_t),
                ("PeakJobMemoryUsed",ctypes.c_size_t)]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize",wintypes.DWORD),
                ("cntUsage",wintypes.DWORD),
                ("th32ThreadID",wintypes.DWORD),
                ("th32OwnerProcessID",wintypes.DWORD),
                ("tpBasePri",wintypes.LONG),
                ("tpDeltaPri",wintypes.LONG),
                ("dwFlags",wintypes.DWORD)]


kernel = ctypes.WinDLL("kernel32",use_last_error=True)
kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p,wintypes.LPCWSTR)
kernel.CreateJobObjectW.restype = wintypes.HANDLE
kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD)
kernel.SetInformationJobObject.restype = wintypes.BOOL
kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE,wintypes.HANDLE)
kernel.AssignProcessToJobObject.restype = wintypes.BOOL
kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD,wintypes.DWORD)
kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel.Thread32First.argtypes = (wintypes.HANDLE,ctypes.POINTER(THREADENTRY32))
kernel.Thread32First.restype = wintypes.BOOL
kernel.Thread32Next.argtypes = (wintypes.HANDLE,ctypes.POINTER(THREADENTRY32))
kernel.Thread32Next.restype = wintypes.BOOL
kernel.OpenThread.argtypes = (wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)
kernel.OpenThread.restype = wintypes.HANDLE
kernel.ResumeThread.argtypes = (wintypes.HANDLE,)
kernel.ResumeThread.restype = wintypes.DWORD
kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel.CloseHandle.restype = wintypes.BOOL


def _check(ok, action):
    if not ok:
        raise OSError(ctypes.get_last_error(), f"Windows {action} failed")


class Job:
    def __init__(self):
        self.handle = None
        self.handle = kernel.CreateJobObjectW(None,None)
        _check(self.handle,"CreateJobObject")
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            _check(kernel.SetInformationJobObject(self.handle,JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                                   ctypes.byref(info),ctypes.sizeof(info)),"SetInformationJobObject")
        except Exception:
            self.close()
            raise

    def close(self):
        if self.handle:
            kernel.CloseHandle(self.handle)
            self.handle = None

    def __del__(self):
        self.close()


def _resume_primary_thread(pid):
    snapshot = kernel.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD,0)
    _check(snapshot != INVALID_HANDLE_VALUE,"CreateToolhelp32Snapshot")
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel.Thread32First(snapshot,ctypes.byref(entry))
        while found:
            if entry.th32OwnerProcessID == pid:
                thread = kernel.OpenThread(THREAD_SUSPEND_RESUME,False,entry.th32ThreadID)
                _check(thread,"OpenThread")
                try:
                    if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                        _check(False,"ResumeThread")
                    return
                finally:
                    kernel.CloseHandle(thread)
            found = kernel.Thread32Next(snapshot,ctypes.byref(entry))
        raise OSError(f"Suspended process {pid} has no thread")
    finally:
        kernel.CloseHandle(snapshot)


def spawn(args,cwd):
    job = Job()
    proc = None
    try:
        proc = subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,cwd=cwd,creationflags=CREATE_SUSPENDED)
        _check(kernel.AssignProcessToJobObject(job.handle,wintypes.HANDLE(proc._handle)),"AssignProcessToJobObject")
        _resume_primary_thread(proc.pid)
        return proc,job
    except Exception:
        job.close()
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            for pipe in (proc.stdin,proc.stdout,proc.stderr):
                if pipe:
                    pipe.close()
        raise
