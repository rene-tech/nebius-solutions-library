/* Static root-owned gate for the privileged capsule installer.
 * No Python instruction executes until the exact installer source and the
 * single-file static/frozen Python runtime match digests compiled into this
 * independently accepted executable. */
#define _GNU_SOURCE

#include <elf.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/mman.h>
#include <sys/types.h>
#include <unistd.h>

#include "fs2-sha256.h"

#ifndef FS2_EXPECTED_INSTALLER_SOURCE_SHA256
#error "FS2_EXPECTED_INSTALLER_SOURCE_SHA256 is required"
#endif
#ifndef FS2_EXPECTED_INSTALLER_PYTHON_SHA256
#error "FS2_EXPECTED_INSTALLER_PYTHON_SHA256 is required"
#endif
#ifndef FS2_EXPECTED_INSTALLER_FROZEN_RUNTIME_REVIEW_SHA256
#error "FS2_EXPECTED_INSTALLER_FROZEN_RUNTIME_REVIEW_SHA256 is required"
#endif

extern char **environ;

static const char *const fixed_launcher =
    "/usr/local/sbin/fs2-install-public-edge-capsule";
static const char *const fixed_source =
    "/usr/local/libexec/fs2-public-edge-installer.py";
static const char *const fixed_python =
    "/usr/local/libexec/fs2-public-edge-installer-python-static";

static void die(const char *message) {
  fprintf(stderr, "public-edge capsule installer gate: %s\n", message);
  _exit(126);
}

static void protected_directory(const char *path) {
  struct stat details;
  if (lstat(path, &details) != 0 || !S_ISDIR(details.st_mode) ||
      details.st_uid != 0 || (details.st_mode & 0022) != 0)
    die("fixed parent directory is not root-owned and protected");
}

static int protected_file(const char *path, mode_t mode) {
  int descriptor = open(path, O_RDONLY | O_NOFOLLOW);
  struct stat details;
  if (descriptor < 0 || fstat(descriptor, &details) != 0 ||
      !S_ISREG(details.st_mode) || details.st_uid != 0 || details.st_gid != 0 ||
      (details.st_mode & 07777) != mode)
    die("fixed installer component has unsafe ownership or mode");
  if (fcntl(descriptor, F_SETFD, 0) != 0)
    die("cannot retain fixed installer component descriptor");
  return descriptor;
}

static int valid_digest(const char *value) {
  if (strlen(value) != 64U) return 0;
  for (size_t index = 0; index < 64U; ++index)
    if (!((value[index] >= '0' && value[index] <= '9') ||
          (value[index] >= 'a' && value[index] <= 'f')))
      return 0;
  return 1;
}

static void require_digest(int descriptor, const char *expected) {
  if (!valid_digest(expected)) die("compiled installer digest is malformed");
  struct stat before, after;
  if (fstat(descriptor, &before) != 0 || lseek(descriptor, 0, SEEK_SET) != 0)
    die("cannot inspect fixed installer component");
  struct fs2_sha256 context;
  fs2_sha256_init(&context);
  unsigned char buffer[32768];
  for (;;) {
    ssize_t length = read(descriptor, buffer, sizeof(buffer));
    if (length < 0) die("cannot hash fixed installer component");
    if (length == 0) break;
    fs2_sha256_update(&context, buffer, (size_t)length);
  }
  unsigned char value[32];
  char encoded[65];
  static const char alphabet[] = "0123456789abcdef";
  fs2_sha256_final(&context, value);
  for (size_t index = 0; index < 32U; ++index) {
    encoded[index * 2] = alphabet[value[index] >> 4];
    encoded[index * 2 + 1] = alphabet[value[index] & 15U];
  }
  encoded[64] = '\0';
  if (fstat(descriptor, &after) != 0 ||
      before.st_dev != after.st_dev || before.st_ino != after.st_ino ||
      before.st_size != after.st_size || before.st_mtim.tv_sec != after.st_mtim.tv_sec ||
      before.st_mtim.tv_nsec != after.st_mtim.tv_nsec ||
      before.st_ctim.tv_sec != after.st_ctim.tv_sec ||
      before.st_ctim.tv_nsec != after.st_ctim.tv_nsec ||
      strcmp(encoded, expected) != 0 || lseek(descriptor, 0, SEEK_SET) != 0)
    die("fixed installer component changed or is not independently accepted");
}

static void require_static_python(int descriptor) {
  Elf64_Ehdr header;
  if (pread(descriptor, &header, sizeof(header), 0) != (ssize_t)sizeof(header) ||
      memcmp(header.e_ident, ELFMAG, SELFMAG) != 0 ||
      header.e_ident[EI_CLASS] != ELFCLASS64 ||
      header.e_phentsize != sizeof(Elf64_Phdr))
    die("installer Python is not ELF64");
  for (Elf64_Half index = 0; index < header.e_phnum; ++index) {
    Elf64_Phdr program;
    off_t offset = (off_t)header.e_phoff + (off_t)index * sizeof(program);
    if (pread(descriptor, &program, sizeof(program), offset) !=
        (ssize_t)sizeof(program))
      die("installer Python program headers cannot be read");
    if (program.p_type == PT_INTERP || program.p_type == PT_DYNAMIC)
      die("installer Python must be a single-file static/frozen executable");
  }
  struct stat details;
  const char *expected = FS2_EXPECTED_INSTALLER_FROZEN_RUNTIME_REVIEW_SHA256;
  if (!valid_digest(expected) || fstat(descriptor, &details) != 0 ||
      details.st_size <= 0)
    die("installer frozen-runtime closure review digest is malformed");
  /* Receipt binding only. The external reviewer must enumerate and inspect
   * the frozen-module closure of the exact whole-binary digest. */
  char marker[96];
  int marker_length = snprintf(marker, sizeof(marker),
      "FS2_FROZEN_RUNTIME_REVIEW_SHA256=%s", expected);
  void *mapped = mmap(NULL, (size_t)details.st_size, PROT_READ, MAP_PRIVATE,
                      descriptor, 0);
  if (marker_length <= 0 || (size_t)marker_length >= sizeof(marker) ||
      mapped == MAP_FAILED)
    die("cannot inspect installer frozen-runtime closure review marker");
  void *first = memmem(mapped, (size_t)details.st_size, marker,
                       (size_t)marker_length);
  void *second = first == NULL ? NULL : memmem(
      (unsigned char *)first + marker_length,
      (size_t)details.st_size -
          ((size_t)((unsigned char *)first - (unsigned char *)mapped) +
           (size_t)marker_length), marker, (size_t)marker_length);
  if (munmap(mapped, (size_t)details.st_size) != 0 || first == NULL ||
      second != NULL)
    die("installer Python lacks one exact frozen-runtime closure review marker");
}

int main(int argc, char **argv) {
  if (geteuid() != 0 || getuid() != 0)
    die("installer gate requires an explicit root execution context");
  protected_directory("/usr");
  protected_directory("/usr/local");
  protected_directory("/usr/local/sbin");
  protected_directory("/usr/local/libexec");
  int self = protected_file(fixed_launcher, 0555);
  struct stat self_details, running_details;
  /* /proc/self/exe is a Linux magic symlink; O_NOFOLLOW would always return
   * ELOOP. The fixed path is protected above and inode equality is the gate. */
  int running = open("/proc/self/exe", O_RDONLY);
  if (running < 0 || fstat(self, &self_details) != 0 ||
      fstat(running, &running_details) != 0 ||
      self_details.st_dev != running_details.st_dev ||
      self_details.st_ino != running_details.st_ino)
    die("running installer gate is not the fixed accepted executable");
  close(self);
  close(running);

  int source = protected_file(fixed_source, 0444);
  int python = protected_file(fixed_python, 0555);
  require_digest(source, FS2_EXPECTED_INSTALLER_SOURCE_SHA256);
  require_digest(python, FS2_EXPECTED_INSTALLER_PYTHON_SHA256);
  require_static_python(python);

  char source_path[64], python_path[64];
  snprintf(source_path, sizeof(source_path), "/proc/self/fd/%d", source);
  snprintf(python_path, sizeof(python_path), "/proc/self/fd/%d", python);
  static const char *const frozen_entry =
      "import sys;p=sys.argv.pop(1);sys.argv[0]='"
      "/usr/local/libexec/fs2-public-edge-installer.py';sys.path[:]=[];"
      "b=open(p,'rb',buffering=0).read();exec(compile(b,'capsule-installer','exec'),"
      "{'__name__':'__main__','__file__':'/usr/local/libexec/fs2-public-edge-installer.py'})";
  if (clearenv() != 0 || setenv("HOME", "/nonexistent", 1) != 0 ||
      setenv("PATH", "/usr/bin:/bin", 1) != 0 ||
      setenv("LANG", "C.UTF-8", 1) != 0 ||
      setenv("LC_ALL", "C.UTF-8", 1) != 0)
    die("cannot establish installer environment");
  char **child = calloc((size_t)argc + 9U, sizeof(char *));
  if (child == NULL) die("cannot allocate installer arguments");
  size_t output = 0;
  child[output++] = python_path;
  child[output++] = "-I";
  child[output++] = "-S";
  child[output++] = "-B";
  child[output++] = "-c";
  child[output++] = (char *)frozen_entry;
  child[output++] = source_path;
  for (int index = 1; index < argc; ++index) child[output++] = argv[index];
  child[output] = NULL;
  execve(python_path, child, environ);
  die(strerror(errno));
}
