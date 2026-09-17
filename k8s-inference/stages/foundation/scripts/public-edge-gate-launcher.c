/*
 * Public-edge trust-boundary launcher.
 *
 * Integration must compile this translation unit as a static PIE, install it
 * root-owned and mode 0555 at the fixed path below, and independently attest
 * the resulting binary before enrolling a provider observer.  The launcher
 * rejects a dynamically linked or writable copy, clears the ambient process
 * environment before Python starts, and executes only a SHA-256-verified
 * in-memory snapshot of the requested Python source.
 */
#error "obsolete caller-selected launcher; build public-edge-capsule-launcher.c only"
#define _GNU_SOURCE

#include <elf.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

extern char **environ;

static const char *const launcher_path =
    "/usr/local/libexec/fs2-public-edge-gate-launcher";

static const char *const gate_environment[] = {
    "FS2_EDGE_GATE_STAGE",
    "FS2_EDGE_GATE_PLANNED_AT",
    "FS2_EDGE_GATE_MAX_PLAN_AGE_SECONDS",
    "FS2_EDGE_GATE_RUN_ROOT",
    "FS2_EDGE_GATE_KUBECONFIG",
    "FS2_EDGE_GATE_KUBE_CONTEXT",
    "FS2_EDGE_GATE_CLUSTER_ID",
    "FS2_EDGE_GATE_PROJECT_ID",
    "FS2_EDGE_GATE_KUBE_SYSTEM_UID",
    "FS2_EDGE_GATE_NODE_GROUP_ID",
    "FS2_EDGE_GATE_RUN_ID",
    "FS2_EDGE_GATE_EXPECTED_NODE_COUNT",
    "FS2_EDGE_GATE_MINIMUM_DOMAINS",
    "FS2_EDGE_GATE_NODE_SELECTOR_JSON",
    "FS2_EDGE_GATE_VERIFIER_SHA256",
    "FS2_EDGE_GATE_POLICY_SHA256",
    "FS2_EDGE_GATE_BINDING_SHA256",
    "FS2_EDGE_GATE_MEMBERSHIP_TRUST_SHA256",
    "FS2_EDGE_GATE_PROVIDER_ADAPTER_TRUST_SHA256",
    NULL,
};

static const char bootstrap[] =
    "import hashlib,os,stat,sys\n"
    "path,expected,mode,*rest=sys.argv[1:]\n"
    "if not os.path.isabs(path) or len(expected)!=64 or any(c not in '0123456789abcdef' for c in expected): raise SystemExit('invalid verified-source contract')\n"
    "flags=os.O_RDONLY|os.O_CLOEXEC|getattr(os,'O_NOFOLLOW',0)\n"
    "fd=os.open(path,flags)\n"
    "try:\n"
    " before=os.fstat(fd); chunks=[]; total=0\n"
    " if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode)&0o022: raise SystemExit('verified source is not a protected regular file')\n"
    " while True:\n"
    "  part=os.read(fd,65536)\n"
    "  if not part: break\n"
    "  chunks.append(part); total+=len(part)\n"
    "  if total>4194304: raise SystemExit('verified source exceeds 4 MiB')\n"
    " after=os.fstat(fd)\n"
    " if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns): raise SystemExit('verified source changed while read')\n"
    " source=b''.join(chunks)\n"
    "finally: os.close(fd)\n"
    "if hashlib.sha256(source).hexdigest()!=expected: raise SystemExit('verified source digest mismatch')\n"
    "os.environ['FS2_VERIFIED_SOURCE_SHA256']=expected\n"
    "os.environ['FS2_PROTECTED_LAUNCHER']='fs2-public-edge-static-v1'\n"
    "sys.argv=[path]+([] if mode=='--local-exec' else ([mode]+rest if mode!='--operator' else rest))\n"
    "scope={'__name__':'__main__','__file__':path,'__package__':None}\n"
    "exec(compile(source,'sha256:'+expected,'exec'),scope,scope)\n";

static void die(const char *message) {
  fprintf(stderr, "public-edge launcher: %s\n", message);
  _exit(126);
}

static void require_root_directory(const char *path) {
  struct stat details;
  if (stat(path, &details) != 0 || !S_ISDIR(details.st_mode) ||
      details.st_uid != 0 || (details.st_mode & 0022) != 0) {
    die("launcher parent directory is not root-owned and protected");
  }
}

static void require_static_self(void) {
  char resolved[PATH_MAX];
  ssize_t length = readlink("/proc/self/exe", resolved, sizeof(resolved) - 1);
  if (length <= 0 || (size_t)length >= sizeof(resolved))
    die("cannot resolve launcher executable");
  resolved[length] = '\0';
  if (strcmp(resolved, launcher_path) != 0)
    die("launcher is not installed at its fixed path");
  require_root_directory("/usr");
  require_root_directory("/usr/local");
  require_root_directory("/usr/local/libexec");

  int descriptor = open("/proc/self/exe", O_RDONLY | O_CLOEXEC);
  if (descriptor < 0) die("cannot open launcher executable");
  struct stat details;
  if (fstat(descriptor, &details) != 0 || !S_ISREG(details.st_mode) ||
      details.st_uid != 0 || (details.st_mode & 0022) != 0)
    die("launcher executable is not root-owned and protected");

  Elf64_Ehdr header;
  if (pread(descriptor, &header, sizeof(header), 0) != (ssize_t)sizeof(header) ||
      memcmp(header.e_ident, ELFMAG, SELFMAG) != 0 ||
      header.e_ident[EI_CLASS] != ELFCLASS64 ||
      header.e_type != ET_DYN ||
      header.e_phentsize != sizeof(Elf64_Phdr))
    die("launcher is not a supported static PIE ELF64 executable");
  for (Elf64_Half index = 0; index < header.e_phnum; ++index) {
    Elf64_Phdr program;
    off_t offset = (off_t)header.e_phoff + (off_t)index * sizeof(program);
    if (pread(descriptor, &program, sizeof(program), offset) !=
        (ssize_t)sizeof(program))
      die("launcher program headers cannot be read");
    if (program.p_type == PT_INTERP)
      die("launcher must be statically linked");
  }
  close(descriptor);
}

static int is_lower_hex_digest(const char *value) {
  if (value == NULL || strlen(value) != 64) return 0;
  for (size_t index = 0; index < 64; ++index) {
    if (!((value[index] >= '0' && value[index] <= '9') ||
          (value[index] >= 'a' && value[index] <= 'f')))
      return 0;
  }
  return 1;
}

static int is_allowed_secret_name(const char *name) {
  size_t length = strlen(name);
  if (length < 3 || length > 128 || name[0] < 'A' || name[0] > 'Z') return 0;
  for (size_t index = 1; index < length; ++index) {
    char value = name[index];
    if (!((value >= 'A' && value <= 'Z') || (value >= '0' && value <= '9') ||
          value == '_'))
      return 0;
  }
  if (strncmp(name, "LD_", 3) == 0 || strncmp(name, "DYLD_", 5) == 0 ||
      strncmp(name, "PYTHON", 6) == 0 || strncmp(name, "TF_", 3) == 0 ||
      strcmp(name, "HOME") == 0 || strcmp(name, "PATH") == 0 ||
      strstr(name, "PROXY") != NULL || strncmp(name, "NEBIUS_", 7) == 0)
    return 0;
  return 1;
}

int main(int argc, char **argv) {
  if (argc < 4 || argv[1][0] != '/' || !is_lower_hex_digest(argv[2]))
    die("expected absolute source path, digest, and mode");
  if (strcmp(argv[3], "--external") != 0 &&
      strcmp(argv[3], "--receipt-contract") != 0 &&
      strcmp(argv[3], "--local-exec") != 0 &&
      strcmp(argv[3], "--operator") != 0)
    die("unsupported launch mode");
  require_static_self();

  const char *operator_names[64] = {0};
  char *operator_values[64] = {0};
  size_t operator_count = 0;
  int operator_arguments = 4;
  if (strcmp(argv[3], "--operator") == 0) {
    int separator_found = 0;
    for (; operator_arguments < argc; ++operator_arguments) {
      if (strcmp(argv[operator_arguments], "--") == 0) {
        separator_found = 1;
        ++operator_arguments;
        break;
      }
      static const char prefix[] = "--preserve-env=";
      if (strncmp(argv[operator_arguments], prefix, sizeof(prefix) - 1) != 0 ||
          operator_count >= 64)
        die("operator mode requires bounded --preserve-env=NAME entries then --");
      const char *name = argv[operator_arguments] + sizeof(prefix) - 1;
      if (!is_allowed_secret_name(name))
        die("operator environment name is not an allowed secret reference");
      operator_names[operator_count++] = name;
    }
    if (!separator_found)
      die("operator argument separator is missing");
    for (size_t index = 0; index < operator_count; ++index) {
      const char *value = getenv(operator_names[index]);
      if (value != NULL && (operator_values[index] = strdup(value)) == NULL)
        die("cannot snapshot explicit operator secret environment");
    }
  }

  char *saved[sizeof(gate_environment) / sizeof(gate_environment[0])] = {0};
  for (size_t index = 0; gate_environment[index] != NULL; ++index) {
    const char *value = getenv(gate_environment[index]);
    if (value != NULL && (saved[index] = strdup(value)) == NULL)
      die("cannot snapshot allowed environment");
  }
  if (clearenv() != 0 || setenv("HOME", "/nonexistent", 1) != 0 ||
      setenv("PATH", "/usr/bin:/bin", 1) != 0 ||
      setenv("LANG", "C.UTF-8", 1) != 0 ||
      setenv("LC_ALL", "C.UTF-8", 1) != 0)
    die("cannot establish fixed environment");
  for (size_t index = 0; index < operator_count; ++index) {
    if (operator_values[index] != NULL) {
      if (setenv(operator_names[index], operator_values[index], 1) != 0)
        die("cannot restore explicit operator secret environment");
      free(operator_values[index]);
    }
  }
  for (size_t index = 0; gate_environment[index] != NULL; ++index) {
    if (saved[index] != NULL) {
      if (setenv(gate_environment[index], saved[index], 1) != 0)
        die("cannot restore allowed environment");
      free(saved[index]);
    }
  }

  size_t child_count = (size_t)argc + 6;
  char **child = calloc(child_count, sizeof(char *));
  if (child == NULL) die("cannot allocate Python arguments");
  size_t output = 0;
  child[output++] = "/usr/bin/python3";
  child[output++] = "-I";
  child[output++] = "-B";
  child[output++] = "-c";
  child[output++] = (char *)bootstrap;
  child[output++] = argv[1];
  child[output++] = argv[2];
  child[output++] = argv[3];
  for (int index = strcmp(argv[3], "--operator") == 0 ? operator_arguments : 4;
       index < argc; ++index)
    child[output++] = argv[index];
  child[output] = NULL;
  execve(child[0], child, environ);
  die(strerror(errno));
}
