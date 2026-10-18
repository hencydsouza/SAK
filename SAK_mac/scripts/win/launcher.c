/* SAK.exe: Windows launcher for the SAK web GUI.
 *
 * Runs  <dir>\python\pythonw.exe -m sak -o "%USERPROFILE%\SAK" gui [args...]
 * from the folder this exe lives in. pythonw has no console, so the GUI server's output goes to
 * <dir>\logs\sak.log (see sak/__main__.py). If the server exits within a few seconds the
 * launcher shows that log's location instead of failing silently.
 *
 * Build (cross, from macOS/Linux):  zig cc -target x86_64-windows-gnu -O2 -municode \
 *     -Wl,--subsystem,windows launcher.c -o SAK.exe
 */
#define WIN32_LEAN_AND_MEAN
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <shellapi.h>
#include <stdio.h>
#include <wchar.h>

#define CMD_MAX 32768

static void fail(const wchar_t *msg) {
    MessageBoxW(NULL, msg, L"Switch Army Knife", MB_OK | MB_ICONERROR);
}

/* Append one argument with Windows command-line quoting (CommandLineToArgvW rules). */
static int append_arg(wchar_t *cmd, size_t cap, const wchar_t *arg) {
    size_t n = wcslen(cmd);
    if (n && n + 1 < cap) cmd[n++] = L' ';
    if (n + 1 >= cap) return 0;
    cmd[n++] = L'"';
    for (const wchar_t *p = arg;; p++) {
        size_t slashes = 0;
        while (*p == L'\\') { slashes++; p++; }
        size_t reps = (*p == L'\0') ? slashes * 2 : (*p == L'"') ? slashes * 2 + 1 : slashes;
        if (n + reps + 3 >= cap) return 0;
        for (size_t i = 0; i < reps; i++) cmd[n++] = L'\\';
        if (*p == L'\0') break;
        cmd[n++] = *p;
    }
    cmd[n++] = L'"';
    cmd[n] = L'\0';
    return 1;
}

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE prev, PWSTR cmdline, int show) {
    (void)inst; (void)prev; (void)cmdline; (void)show;
    wchar_t dir[MAX_PATH], python[MAX_PATH], out[MAX_PATH], msg[1024];
    static wchar_t cmd[CMD_MAX];

    DWORD len = GetModuleFileNameW(NULL, dir, MAX_PATH);
    if (len == 0 || len >= MAX_PATH) { fail(L"Cannot locate SAK.exe."); return 1; }
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (!slash) { fail(L"Cannot locate SAK.exe."); return 1; }
    *slash = L'\0';

    swprintf(python, MAX_PATH, L"%ls\\python\\pythonw.exe", dir);
    if (GetFileAttributesW(python) == INVALID_FILE_ATTRIBUTES) {
        swprintf(msg, 1024, L"Missing %ls\n\nExtract the whole SAK zip and start SAK.exe from that folder.", python);
        fail(msg);
        return 1;
    }

    const wchar_t *home = _wgetenv(L"USERPROFILE");
    swprintf(out, MAX_PATH, L"%ls\\SAK", home && *home ? home : dir);

    cmd[0] = L'\0';
    int ok = append_arg(cmd, CMD_MAX, python) && append_arg(cmd, CMD_MAX, L"-m") &&
             append_arg(cmd, CMD_MAX, L"sak") && append_arg(cmd, CMD_MAX, L"-o") &&
             append_arg(cmd, CMD_MAX, out) && append_arg(cmd, CMD_MAX, L"gui");
    int argc = 0;
    wchar_t **argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    for (int i = 1; ok && argv && i < argc; i++) ok = append_arg(cmd, CMD_MAX, argv[i]);
    if (argv) LocalFree(argv);
    if (!ok) { fail(L"Command line too long."); return 1; }

    STARTUPINFOW si;
    ZeroMemory(&si, sizeof si);
    si.cb = sizeof si;
    PROCESS_INFORMATION pi;
    if (!CreateProcessW(python, cmd, NULL, NULL, FALSE, 0, NULL, dir, &si, &pi)) {
        swprintf(msg, 1024, L"Could not start %ls (error %lu).", python, GetLastError());
        fail(msg);
        return 1;
    }
    CloseHandle(pi.hThread);

    /* The server keeps running (and opens the browser); an exit this early means it failed. */
    DWORD rc = 0;
    if (WaitForSingleObject(pi.hProcess, 5000) == WAIT_OBJECT_0 && GetExitCodeProcess(pi.hProcess, &rc) && rc != 0) {
        swprintf(msg, 1024, L"SAK stopped while starting (exit code %lu).\n\nDetails: %ls\\logs\\sak.log", rc, dir);
        fail(msg);
        CloseHandle(pi.hProcess);
        return (int)rc;
    }
    CloseHandle(pi.hProcess);
    return 0;
}
