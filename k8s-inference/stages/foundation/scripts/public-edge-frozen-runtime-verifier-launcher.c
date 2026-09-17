/* Fixed, independently installed entry point for the offline frozen-runtime
 * verifier.  No shebang, PATH lookup, caller interpreter, or host import path
 * participates before the reviewed frozen Python is measured. */
#define _GNU_SOURCE

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "fs2-sha256.h"
#include "fs2-frozen-runtime.h"

#ifndef FS2_EXPECTED_VERIFIER_SHA256
#error "FS2_EXPECTED_VERIFIER_SHA256 must bind the verifier source"
#endif
#ifndef FS2_EXPECTED_VERIFIER_PYTHON_SHA256
#error "FS2_EXPECTED_VERIFIER_PYTHON_SHA256 must bind the frozen interpreter"
#endif

extern char **environ;

void *__wrap_dlopen(const char *path, int flags) {
  (void)path;
  (void)flags;
  errno = EPERM;
  return NULL;
}

void *__wrap_dlmopen(Lmid_t namespace_id, const char *path, int flags) {
  (void)namespace_id;
  (void)path;
  (void)flags;
  errno = EPERM;
  return NULL;
}

static const char *const fixed_self =
    "/usr/local/libexec/fs2-verify-public-edge-frozen-runtime";
static const char *const fixed_verifier =
    "/usr/local/libexec/fs2-verify-public-edge-frozen-runtime.py";
static const char *const fixed_python =
    "/usr/local/libexec/fs2-public-edge-frozen-verifier-python";

static void die(const char *message) {
  fprintf(stderr, "public-edge frozen verifier launcher: %s\n", message);
  _exit(126);
}

static void protected_parent(const char *path) {
  struct stat details;
  if (lstat(path, &details) != 0 || !S_ISDIR(details.st_mode) ||
      details.st_uid != 0 || (details.st_mode & 0022) != 0)
    die("fixed parent directory is not root-owned and protected");
}

static int protected_file(const char *path, mode_t mode) {
  int descriptor = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  struct stat details;
  if (descriptor < 0 || fstat(descriptor, &details) != 0 ||
      !S_ISREG(details.st_mode) || details.st_uid != 0 || details.st_gid != 0 ||
      (details.st_mode & 07777) != mode)
    die("fixed verifier component is not root-owned with exact mode");
  if (fcntl(descriptor, F_SETFD, 0) != 0)
    die("cannot retain fixed verifier descriptor");
  return descriptor;
}

static void require_digest(int descriptor, const char *expected) {
  struct stat before, after;
  if (strlen(expected) != 64U || fstat(descriptor, &before) != 0 ||
      lseek(descriptor, 0, SEEK_SET) != 0)
    die("accepted verifier digest is malformed");
  struct fs2_sha256 context;
  fs2_sha256_init(&context);
  unsigned char buffer[32768];
  for (;;) {
    ssize_t length = read(descriptor, buffer, sizeof(buffer));
    if (length < 0) die("cannot hash fixed verifier component");
    if (length == 0) break;
    fs2_sha256_update(&context, buffer, (size_t)length);
  }
  unsigned char digest[32];
  char encoded[65];
  static const char alphabet[] = "0123456789abcdef";
  fs2_sha256_final(&context, digest);
  for (size_t index = 0; index < 32U; ++index) {
    encoded[index * 2U] = alphabet[digest[index] >> 4];
    encoded[index * 2U + 1U] = alphabet[digest[index] & 15U];
  }
  encoded[64] = '\0';
  if (fstat(descriptor, &after) != 0 ||
      before.st_dev != after.st_dev || before.st_ino != after.st_ino ||
      before.st_size != after.st_size ||
      before.st_mtim.tv_sec != after.st_mtim.tv_sec ||
      before.st_mtim.tv_nsec != after.st_mtim.tv_nsec ||
      before.st_ctim.tv_sec != after.st_ctim.tv_sec ||
      before.st_ctim.tv_nsec != after.st_ctim.tv_nsec ||
      strcmp(encoded, expected) != 0 || lseek(descriptor, 0, SEEK_SET) != 0)
    die("fixed verifier component differs from independently accepted bytes");
}

int main(int argc, char **argv) {
  if (argc != 5 || strcmp(argv[1], "--python") != 0 ||
      strcmp(argv[3], "--expected-python-sha256") != 0)
    die("expected --python PATH --expected-python-sha256 DIGEST");
  protected_parent("/usr");
  protected_parent("/usr/local");
  protected_parent("/usr/local/libexec");
  int self = protected_file(fixed_self, 0555);
  int running = open("/proc/self/exe", O_RDONLY | O_CLOEXEC);
  struct stat fixed_details, running_details;
  if (running < 0 || fstat(self, &fixed_details) != 0 ||
      fstat(running, &running_details) != 0 ||
      fixed_details.st_dev != running_details.st_dev ||
      fixed_details.st_ino != running_details.st_ino)
    die("running verifier launcher is not the fixed accepted executable");
  close(self);
  close(running);
  int verifier = protected_file(fixed_verifier, 0444);
  int python = protected_file(fixed_python, 0555);
  require_digest(verifier, FS2_EXPECTED_VERIFIER_SHA256);
  require_digest(python, FS2_EXPECTED_VERIFIER_PYTHON_SHA256);
  if (clearenv() != 0 || setenv("HOME", "/nonexistent", 1) != 0 ||
      setenv("PATH", "/nonexistent", 1) != 0 || setenv("LANG", "C", 1) != 0 ||
      setenv("LC_ALL", "C", 1) != 0 ||
      setenv("OPENSSL_CONF", "/dev/null", 1) != 0 ||
      setenv("OPENSSL_MODULES", "/nonexistent", 1) != 0)
    die("cannot establish the fixed verifier environment");
  fs2_require_frozen_runtime(python, die);
  char python_path[64], verifier_path[64];
  snprintf(python_path, sizeof(python_path), "/proc/self/fd/%d", python);
  snprintf(verifier_path, sizeof(verifier_path), "/proc/self/fd/%d", verifier);
  static const char *const entry =
      "import sys;p=sys.argv.pop(1);sys.path[:]=[];sys.path_hooks[:]=[];"
      "sys.path_importer_cache.clear();sys.meta_path[:]=[x for x in sys.meta_path if "
      "getattr(x,'__name__','') in ('BuiltinImporter','FrozenImporter')];"
      "b=open(p,'rb',buffering=0).read();"
      "exec(compile(b,'frozen-runtime-verifier','exec'),"
      "{'__name__':'__main__','__file__':'/usr/local/libexec/fs2-verify-public-edge-frozen-runtime.py'})";
  char *child[] = {python_path, "-I", "-S", "-B", "-c", (char *)entry,
                   verifier_path, argv[1], argv[2], argv[3], argv[4], NULL};
  execve(python_path, child, environ);
  die(strerror(errno));
}
