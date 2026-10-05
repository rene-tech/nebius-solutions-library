/* Fixed diagnostic, never an arbitrary command launcher or limit setter. */
#define _GNU_SOURCE
#include <errno.h>
#include <linux/capability.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <unistd.h>

int main(void) {
    struct __user_cap_header_struct header = {_LINUX_CAPABILITY_VERSION_3, 0};
    struct __user_cap_data_struct data[2] = {{0}, {0}};
    struct rlimit limit;
    if (syscall(SYS_capget, &header, data) != 0 || getrlimit(RLIMIT_MEMLOCK, &limit) != 0)
        return 2;
    uint64_t effective = data[0].effective | ((uint64_t)data[1].effective << 32);
    uint64_t permitted = data[0].permitted | ((uint64_t)data[1].permitted << 32);
    uint64_t inherited = data[0].inheritable | ((uint64_t)data[1].inheritable << 32);
    uint64_t bounding = 0;
    for (int i = 0; i <= CAP_LAST_CAP; ++i)
        if (prctl(PR_CAPBSET_READ, i, 0, 0, 0) == 1) bounding |= (uint64_t)1 << i;
    const size_t bytes = 64 * 1024 * 1024;
    void *memory = malloc(bytes);
    if (!memory) return 2;
    errno = 0;
    int result = mlock(memory, bytes), saved_errno = errno;
    int no_new_privs = prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0);
    int passed = result == 0 && getuid() == 10001 && geteuid() == 10001 &&
                 effective == ((uint64_t)1 << CAP_IPC_LOCK) && permitted == effective &&
                 bounding == effective && inherited == 0 && no_new_privs == 0;
    printf("{\"kind\":\"rdma-memlock-proof\",\"status\":\"%s\",\"uid\":%u,\"euid\":%u,\"cap_effective\":\"%llx\",\"cap_permitted\":\"%llx\",\"cap_bounding\":\"%llx\",\"cap_inheritable\":\"%llx\",\"no_new_privs\":%d,\"rlimit_soft\":%llu,\"rlimit_hard\":%llu,\"locked_bytes\":%zu,\"mlock_errno\":%d,\"ld_library_path_present\":%s}\n",
           passed ? "passed" : "failed", getuid(), geteuid(),
           (unsigned long long)effective, (unsigned long long)permitted,
           (unsigned long long)bounding, (unsigned long long)inherited, no_new_privs,
           (unsigned long long)limit.rlim_cur, (unsigned long long)limit.rlim_max,
           result == 0 ? bytes : 0, saved_errno, getenv("LD_LIBRARY_PATH") ? "true" : "false");
    if (result == 0) munlock(memory, bytes);
    free(memory);
    return passed ? 0 : 2;
}
