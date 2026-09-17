/*
 * Public-edge accepted-release capsule launcher.
 *
 * This program has no caller-controlled source path, digest, interpreter, or
 * executable path. Integration installs an independently accepted release
 * tree and canonical manifest, then installs this static PIE root-owned,
 * setgid to the no-member `fs2-public-edge-capsule` group, at the fixed path
 * below. The manifest and bootstrap are root:group 0440. An ordinary caller
 * cannot read them or manufacture the effective-group proof in a direct
 * Python invocation.
 */
#define _GNU_SOURCE

#include <elf.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <limits.h>
#include <stdint.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include "fs2-sha256.h"

#ifndef FS2_EXPECTED_BOOTSTRAP_SHA256
#error "FS2_EXPECTED_BOOTSTRAP_SHA256 must bind the reviewed bootstrap bytes"
#endif
#ifndef FS2_EXPECTED_PYTHON_SHA256
#error "FS2_EXPECTED_PYTHON_SHA256 must bind the reviewed static/frozen Python"
#endif
#ifndef FS2_EXPECTED_FROZEN_RUNTIME_REVIEW_SHA256
#error "FS2_EXPECTED_FROZEN_RUNTIME_REVIEW_SHA256 must bind an external closure review"
#endif

extern char **environ;

static const char *const launcher_path =
    "/usr/local/libexec/fs2-public-edge-current/launcher";
static const char *const activation_root =
    "/usr/local/libexec/fs2-public-edge-activations";
static const char *const capsule_group = "fs2-public-edge-capsule";
static const char *const expected_bootstrap_sha256 =
    FS2_EXPECTED_BOOTSTRAP_SHA256;
static const char *const expected_python_sha256 = FS2_EXPECTED_PYTHON_SHA256;
static const char *const expected_frozen_runtime_review_sha256 =
    FS2_EXPECTED_FROZEN_RUNTIME_REVIEW_SHA256;

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
    "FS2_EDGE_GATE_MAXIMUM_SURGE_MEMBERS",
    "FS2_EDGE_GATE_NODE_SELECTOR_JSON",
    "FS2_EDGE_GATE_VERIFIER_SHA256",
    "FS2_EDGE_GATE_POLICY_SHA256",
    "FS2_EDGE_GATE_BINDING_SHA256",
    "FS2_EDGE_GATE_CAS_POLICY_SHA256",
    "FS2_EDGE_GATE_CAS_BINDING_SHA256",
    "FS2_EDGE_GATE_BOOTSTRAP_POLICY_SHA256",
    "FS2_EDGE_GATE_BOOTSTRAP_BINDING_SHA256",
    "FS2_EDGE_GATE_BOUNDARY_APPROVAL_API_VERSION",
    "FS2_EDGE_GATE_BOUNDARY_APPROVAL_KIND",
    "FS2_EDGE_GATE_BOUNDARY_APPROVAL_NAME",
    "FS2_EDGE_GATE_BOUNDARY_APPROVAL_RESOURCE",
    "FS2_EDGE_GATE_BOUNDARY_APPROVAL_SHA256",
    "FS2_EDGE_GATE_MEMBERSHIP_TRUST_SHA256",
    "FS2_EDGE_GATE_PROVIDER_ADAPTER_TRUST_SHA256",
    "FS2_KUEUE_CHART_REF",
    "FS2_KUEUE_CHART_DIGEST",
    "FS2_KUEUE_CHART_ARCHIVE_SHA256",
    "FS2_KUEUE_IMAGE",
    "FS2_KUEUE_RUN_ROOT",
    "FS2_KUEUE_CHART_ARCHIVE",
    "FS2_GATE_KUBECONFIG",
    "FS2_GATE_RUN_ROOT",
    "FS2_GATE_KUBE_CONTEXT",
    "FS2_GATE_CLUSTER_ID",
    "FS2_GATE_CLUSTER_NAME",
    "FS2_GATE_KUBE_SYSTEM_UID",
    "FS2_GATE_RUN_ID",
    "FS2_GATE_KUEUE_RELEASE",
    "FS2_GATE_TIMEOUT_SECONDS",
    "FS2_GATE_RETRY_SECONDS",
    "FS2_CLEANUP_KUBECONFIG",
    "FS2_CLEANUP_RUN_ROOT",
    "FS2_CLEANUP_KUBE_CONTEXT",
    "FS2_CLEANUP_CLUSTER_ID",
    "FS2_CLEANUP_CLUSTER_NAME",
    "FS2_CLEANUP_KUBE_SYSTEM_UID",
    "FS2_CLEANUP_RUN_ID",
    "FS2_CLEANUP_KUEUE_RELEASE",
    "FS2_CLEANUP_TIMEOUT_SECONDS",
    "FS2_CLEANUP_RETRY_SECONDS",
    "FS2_JOBSET_CHART_ARCHIVE",
    "FS2_JOBSET_CHART_ARCHIVE_SHA256",
    "FS2_JOBSET_CHART_DIGEST",
    "FS2_JOBSET_CHART_REF",
    "FS2_JOBSET_CHART_VERSION",
    "FS2_JOBSET_CLUSTER_ID",
    "FS2_JOBSET_CONTEXT",
    "FS2_JOBSET_CONTROLLER",
    "FS2_JOBSET_IMAGE",
    "FS2_JOBSET_IMAGE_DIGEST",
    "FS2_JOBSET_KUBECONFIG",
    "FS2_JOBSET_KUBERNETES_MINOR",
    "FS2_JOBSET_NAMESPACE",
    "FS2_JOBSET_RENDERED_IMAGE",
    "FS2_JOBSET_RUN_ROOT",
    "FS2_JOBSET_TIMEOUT_SECONDS",
    "FS2_JOBSET_VERIFY_RUN_ROOT",
    "FS2_CAPSULE_DELEGATED_AUTH_FD",
    "FS2_CAPSULE_DELEGATED_AUTH_ENVELOPE",
    "FS2_CAPSULE_NEBIUS_PROFILE",
    NULL,
};

static const char *const operator_secret_slots[] = {
    "grafana_username",
    "grafana_password",
    "ngc_api_key",
    "nvcr_dockerconfig",
    NULL,
};

#define MAX_OPERATOR_SECRET_BYTES (1024U * 1024U)
#define MAX_OPERATOR_SECRET_REQUEST 256

static void die(const char *message) {
  fprintf(stderr, "public-edge capsule launcher: %s\n", message);
  _exit(126);
}

static void require_root_directory(const char *path) {
  struct stat details;
  if (lstat(path, &details) != 0 || !S_ISDIR(details.st_mode) ||
      details.st_uid != 0 || (details.st_mode & 0022) != 0)
    die("fixed parent directory is not root-owned and protected");
}

static int open_protected_at(int directory, const char *name, gid_t group,
                             mode_t mode) {
  int descriptor = openat(directory, name, O_RDONLY | O_NOFOLLOW);
  if (descriptor < 0) die("cannot open fixed capsule file");
  struct stat details;
  if (fstat(descriptor, &details) != 0 || !S_ISREG(details.st_mode) ||
      details.st_uid != 0 || details.st_gid != group ||
      (details.st_mode & 07777) != mode)
    die("fixed capsule file is not root-owned with the required protection");
  if (fcntl(descriptor, F_SETFD, 0) != 0)
    die("cannot make protected capsule descriptor inheritable");
  return descriptor;
}

static int lowercase_digest(const char *value) {
  if (strlen(value) != 64U) return 0;
  for (size_t index = 0; index < 64U; ++index)
    if (!((value[index] >= '0' && value[index] <= '9') ||
          (value[index] >= 'a' && value[index] <= 'f')))
      return 0;
  return 1;
}

static void require_fd_digest(int descriptor, const char *expected,
                              const char *label) {
  if (!lowercase_digest(expected)) die("compiled accepted digest is malformed");
  struct stat before, after;
  if (fstat(descriptor, &before) != 0 || lseek(descriptor, 0, SEEK_SET) != 0)
    die("cannot inspect accepted capsule input");
  struct fs2_sha256 context;
  fs2_sha256_init(&context);
  unsigned char buffer[32768];
  for (;;) {
    ssize_t length = read(descriptor, buffer, sizeof(buffer));
    if (length < 0) die("cannot hash accepted capsule input");
    if (length == 0) break;
    fs2_sha256_update(&context, buffer, (size_t)length);
  }
  unsigned char digest[32];
  char encoded[65];
  static const char alphabet[] = "0123456789abcdef";
  fs2_sha256_final(&context, digest);
  for (size_t index = 0; index < 32U; ++index) {
    encoded[index * 2] = alphabet[digest[index] >> 4];
    encoded[index * 2 + 1] = alphabet[digest[index] & 15U];
  }
  encoded[64] = '\0';
  if (fstat(descriptor, &after) != 0 ||
      before.st_dev != after.st_dev || before.st_ino != after.st_ino ||
      before.st_size != after.st_size || before.st_mtim.tv_sec != after.st_mtim.tv_sec ||
      before.st_mtim.tv_nsec != after.st_mtim.tv_nsec ||
      before.st_ctim.tv_sec != after.st_ctim.tv_sec ||
      before.st_ctim.tv_nsec != after.st_ctim.tv_nsec ||
      strcmp(encoded, expected) != 0 || lseek(descriptor, 0, SEEK_SET) != 0) {
    (void)label;
    die("accepted capsule input changed or differs from compiled digest");
  }
}

static void require_static_frozen_python(int descriptor) {
  Elf64_Ehdr header;
  if (pread(descriptor, &header, sizeof(header), 0) != (ssize_t)sizeof(header) ||
      memcmp(header.e_ident, ELFMAG, SELFMAG) != 0 ||
      header.e_ident[EI_CLASS] != ELFCLASS64 ||
      header.e_phentsize != sizeof(Elf64_Phdr))
    die("accepted Python runtime is not ELF64");
  for (Elf64_Half index = 0; index < header.e_phnum; ++index) {
    Elf64_Phdr program;
    off_t offset = (off_t)header.e_phoff + (off_t)index * sizeof(program);
    if (pread(descriptor, &program, sizeof(program), offset) !=
        (ssize_t)sizeof(program))
      die("accepted Python runtime program headers cannot be read");
    if (program.p_type == PT_INTERP || program.p_type == PT_DYNAMIC)
      die("accepted Python runtime must be a single-file static executable");
  }
  struct stat details;
  if (!lowercase_digest(expected_frozen_runtime_review_sha256) ||
      fstat(descriptor, &details) != 0 || details.st_size <= 0)
    die("frozen-runtime closure review digest is malformed");
  /* Receipt binding only. The external reviewer must enumerate and inspect
   * the frozen-module closure of the exact whole-binary digest. */
  char marker[96];
  int marker_length = snprintf(
      marker, sizeof(marker), "FS2_FROZEN_RUNTIME_REVIEW_SHA256=%s",
      expected_frozen_runtime_review_sha256);
  void *mapped = mmap(NULL, (size_t)details.st_size, PROT_READ, MAP_PRIVATE,
                      descriptor, 0);
  if (marker_length <= 0 || (size_t)marker_length >= sizeof(marker) ||
      mapped == MAP_FAILED)
    die("cannot inspect frozen-runtime closure review marker");
  void *first = memmem(mapped, (size_t)details.st_size, marker,
                       (size_t)marker_length);
  void *second = first == NULL ? NULL : memmem(
      (unsigned char *)first + marker_length,
      (size_t)details.st_size -
          ((size_t)((unsigned char *)first - (unsigned char *)mapped) +
           (size_t)marker_length),
      marker, (size_t)marker_length);
  if (munmap(mapped, (size_t)details.st_size) != 0 || first == NULL ||
      second != NULL)
    die("static Python lacks one exact frozen-runtime closure review marker");
}

static gid_t require_capsule_identity(void) {
  gid_t real_gid, effective_gid, saved_gid;
  if (getresgid(&real_gid, &effective_gid, &saved_gid) != 0 ||
      effective_gid == real_gid || effective_gid != saved_gid)
    die("launcher lacks the dedicated setgid capsule identity");
  struct group *group = getgrgid(effective_gid);
  if (group == NULL || strcmp(group->gr_name, capsule_group) != 0)
    die("launcher effective group is not the fixed capsule group");
  int count = getgroups(0, NULL);
  if (count < 0) die("cannot inspect caller supplementary groups");
  gid_t *groups = count == 0 ? NULL : calloc((size_t)count, sizeof(gid_t));
  if (count > 0 && (groups == NULL || getgroups(count, groups) != count))
    die("cannot inspect caller supplementary groups");
  for (int index = 0; index < count; ++index)
    if (groups[index] == effective_gid)
      die("capsule group must have no ordinary members");
  free(groups);
  return effective_gid;
}

static int require_static_self(gid_t group) {
  char resolved[PATH_MAX];
  ssize_t length = readlink("/proc/self/exe", resolved, sizeof(resolved) - 1);
  if (length <= 0 || (size_t)length >= sizeof(resolved))
    die("cannot resolve launcher executable");
  resolved[length] = '\0';
  require_root_directory("/usr");
  require_root_directory("/usr/local");
  require_root_directory("/usr/local/libexec");
  require_root_directory(activation_root);

  size_t root_length = strlen(activation_root);
  if (strncmp(resolved, activation_root, root_length) != 0 ||
      resolved[root_length] != '/' || strchr(resolved + root_length + 1, '/') == NULL)
    die("launcher is outside the versioned activation root");
  char unit_path[PATH_MAX];
  if (strlen(resolved) >= sizeof(unit_path))
    die("launcher activation path is too long");
  strcpy(unit_path, resolved);
  char *leaf = strrchr(unit_path, '/');
  if (leaf == NULL || strcmp(leaf + 1, "launcher") != 0)
    die("launcher activation leaf is not canonical");
  *leaf = '\0';

  int unit_directory = open(unit_path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW);
  if (unit_directory < 0) die("cannot open launcher activation unit");
  struct stat unit_details;
  if (fstat(unit_directory, &unit_details) != 0 ||
      !S_ISDIR(unit_details.st_mode) || unit_details.st_uid != 0 ||
      unit_details.st_gid != group ||
      (unit_details.st_mode & 07777) != 0550)
    die("launcher activation unit is not root-owned and protected");

  int descriptor = open("/proc/self/exe", O_RDONLY | O_CLOEXEC);
  if (descriptor < 0) die("cannot open launcher executable");
  struct stat details;
  if (fstat(descriptor, &details) != 0 || !S_ISREG(details.st_mode) ||
      details.st_uid != 0 || details.st_gid != group ||
      (details.st_mode & 07777) != 02755)
    die("launcher must be root:capsule mode 2755");

  int fixed_descriptor = open(launcher_path, O_RDONLY);
  struct stat fixed_details;
  if (fixed_descriptor < 0 || fstat(fixed_descriptor, &fixed_details) != 0 ||
      fixed_details.st_dev != details.st_dev ||
      fixed_details.st_ino != details.st_ino)
    die("fixed activation does not select this exact launcher");
  close(fixed_descriptor);

  Elf64_Ehdr header;
  if (pread(descriptor, &header, sizeof(header), 0) != (ssize_t)sizeof(header) ||
      memcmp(header.e_ident, ELFMAG, SELFMAG) != 0 ||
      header.e_ident[EI_CLASS] != ELFCLASS64 || header.e_type != ET_DYN ||
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
  return unit_directory;
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
      strstr(name, "PROXY") != NULL || strncmp(name, "NEBIUS_", 7) == 0 ||
      strncmp(name, "FS2_CAPSULE_", 12) == 0 ||
      strncmp(name, "AWS_", 4) == 0 || strncmp(name, "GITHUB_", 7) == 0 ||
      strncmp(name, "OPENAI_", 7) == 0)
    return 0;
  return 1;
}

static int operator_secret_slot(const char *name) {
  for (int index = 0; operator_secret_slots[index] != NULL; ++index)
    if (strcmp(name, operator_secret_slots[index]) == 0) return index;
  return -1;
}

static void send_secret_response(int socket_fd, uint8_t status, int secret_fd) {
  struct iovec body = {.iov_base = &status, .iov_len = sizeof(status)};
  char control[CMSG_SPACE(sizeof(int))] = {0};
  struct msghdr message = {
      .msg_iov = &body,
      .msg_iovlen = 1,
  };
  if (secret_fd >= 0) {
    message.msg_control = control;
    message.msg_controllen = sizeof(control);
    struct cmsghdr *header = CMSG_FIRSTHDR(&message);
    header->cmsg_level = SOL_SOCKET;
    header->cmsg_type = SCM_RIGHTS;
    header->cmsg_len = CMSG_LEN(sizeof(int));
    memcpy(CMSG_DATA(header), &secret_fd, sizeof(secret_fd));
  }
  if (sendmsg(socket_fd, &message, MSG_NOSIGNAL) != (ssize_t)sizeof(status))
    die("cannot return a sealed operator secret descriptor");
}

static int sealed_secret(const char *slot, const char *value) {
  size_t length = strlen(value);
  if (length == 0 || length > MAX_OPERATOR_SECRET_BYTES)
    die("operator secret is empty or exceeds its bounded size");
  int descriptor = memfd_create(slot, MFD_CLOEXEC | MFD_ALLOW_SEALING);
  if (descriptor < 0) die("cannot create a sealed operator secret descriptor");
  size_t offset = 0;
  while (offset < length) {
    ssize_t written = write(descriptor, value + offset, length - offset);
    if (written <= 0) die("cannot write a sealed operator secret descriptor");
    offset += (size_t)written;
  }
  int seals = F_SEAL_SEAL | F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE;
  if (lseek(descriptor, 0, SEEK_SET) != 0 ||
      fcntl(descriptor, F_ADD_SEALS, seals) != 0 ||
      (fcntl(descriptor, F_GET_SEALS) & seals) != seals)
    die("cannot seal an operator secret descriptor");
  return descriptor;
}

static void serve_operator_secrets(int socket_fd) {
  unsigned int served = 0;
  for (;;) {
    char request[MAX_OPERATOR_SECRET_REQUEST] = {0};
    ssize_t length = recv(socket_fd, request, sizeof(request), 0);
    if (length == 0) return;
    if (length < 3 || (size_t)length >= sizeof(request) ||
        request[length - 1] != '\0')
      die("operator secret request is malformed");
    char *environment_name = request + strlen(request) + 1;
    if (environment_name >= request + length ||
        environment_name + strlen(environment_name) + 1 != request + length)
      die("operator secret request does not contain one exact name");
    int slot = operator_secret_slot(request);
    if (slot < 0 || (served & (1U << (unsigned int)slot)) != 0 ||
        !is_allowed_secret_name(environment_name))
      die("operator secret request is outside the four-slot contract");
    served |= 1U << (unsigned int)slot;
    const char *value = getenv(environment_name);
    if (value == NULL) {
      send_secret_response(socket_fd, 0, -1);
      continue;
    }
    int descriptor = sealed_secret(request, value);
    send_secret_response(socket_fd, 1, descriptor);
    close(descriptor);
  }
}

static int supported_mode(const char *source, const char *mode) {
  if (strcmp(source, "inference-stack") == 0)
    return strcmp(mode, "operator") == 0;
  if (strcmp(source, "public-edge-verifier") == 0)
    return strcmp(mode, "external") == 0 ||
           strcmp(mode, "receipt-contract") == 0 ||
           strcmp(mode, "local-exec") == 0;
  if (strcmp(source, "edge-client-identity-verifier") == 0 ||
      strcmp(source, "jobset-chart-materializer") == 0)
    return strcmp(mode, "external") == 0;
  if (strcmp(source, "jobset-api-gate") == 0 ||
      strcmp(source, "jobset-crd-upgrade") == 0 ||
      strcmp(source, "jobset-release-verifier") == 0)
    return strcmp(mode, "local-exec") == 0;
  return (strcmp(source, "kueue-materializer") == 0 ||
          strcmp(source, "kueue-admission-gate") == 0 ||
          strcmp(source, "kueue-destroy-cleanup") == 0) &&
         strcmp(mode, "local-exec") == 0;
}

int main(int argc, char **argv) {
  if (argc < 3 || !supported_mode(argv[1], argv[2]))
    die("expected a supported logical source and mode");
  gid_t group = require_capsule_identity();
  int activation_directory = require_static_self(group);

  int forwarded_arguments = 3;
  if (strcmp(argv[1], "inference-stack") == 0) {
    if (forwarded_arguments >= argc || strcmp(argv[forwarded_arguments], "--") != 0)
      die("operator mode accepts only -- followed by operator arguments");
    ++forwarded_arguments;
  }

  char *saved[sizeof(gate_environment) / sizeof(gate_environment[0])] = {0};
  if (strcmp(argv[1], "inference-stack") != 0) {
    for (size_t index = 0; gate_environment[index] != NULL; ++index) {
      const char *value = getenv(gate_environment[index]);
      if (value != NULL && (saved[index] = strdup(value)) == NULL)
        die("cannot snapshot allowed gate environment");
    }
  }

  int bootstrap_fd =
      open_protected_at(activation_directory, "bootstrap.py", group, 0440);
  int manifest_fd =
      open_protected_at(activation_directory, "manifest.json", group, 0440);
  int python_fd =
      open_protected_at(activation_directory, "python3", group, 0550);
  /* These checks complete before the interpreter executes. The accepted
   * Python is a single-file static build whose required stdlib is frozen;
   * the -c prelude below clears sys.path before the reviewed bootstrap runs. */
  require_fd_digest(bootstrap_fd, expected_bootstrap_sha256, "bootstrap");
  require_fd_digest(python_fd, expected_python_sha256, "Python runtime");
  require_static_frozen_python(python_fd);
  close(activation_directory);
  int launcher_fd = open("/proc/self/exe", O_RDONLY);
  if (launcher_fd < 0 || fcntl(launcher_fd, F_SETFD, 0) != 0)
    die("cannot retain the verified launcher descriptor");
  char bootstrap_reference[64], python_reference[64], manifest_text[32],
      python_text[32], launcher_text[32], bootstrap_text[32];
  snprintf(bootstrap_reference, sizeof(bootstrap_reference), "/proc/self/fd/%d",
           bootstrap_fd);
  snprintf(python_reference, sizeof(python_reference), "/proc/self/fd/%d",
           python_fd);
  snprintf(manifest_text, sizeof(manifest_text), "%d", manifest_fd);
  snprintf(python_text, sizeof(python_text), "%d", python_fd);
  snprintf(launcher_text, sizeof(launcher_text), "%d", launcher_fd);
  snprintf(bootstrap_text, sizeof(bootstrap_text), "%d", bootstrap_fd);

  int secret_socket = -1;
  pid_t operator_child = -1;
  char secret_socket_text[32] = {0};
  if (strcmp(argv[1], "inference-stack") == 0) {
    int sockets[2];
    if (socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, sockets) != 0)
      die("cannot create the operator secret broker socket");
    if (fcntl(sockets[1], F_SETFD, 0) != 0)
      die("cannot inherit the operator secret broker socket");
    operator_child = fork();
    if (operator_child < 0) die("cannot fork the operator secret broker");
    if (operator_child > 0) {
      close(sockets[1]);
      serve_operator_secrets(sockets[0]);
      close(sockets[0]);
      int child_status = 0;
      if (waitpid(operator_child, &child_status, 0) != operator_child)
        die("cannot collect the accepted operator process");
      if (WIFEXITED(child_status)) _exit(WEXITSTATUS(child_status));
      if (WIFSIGNALED(child_status)) {
        signal(WTERMSIG(child_status), SIG_DFL);
        raise(WTERMSIG(child_status));
      }
      _exit(126);
    }
    close(sockets[0]);
    secret_socket = sockets[1];
    snprintf(secret_socket_text, sizeof(secret_socket_text), "%d", secret_socket);
  }

  if (clearenv() != 0 || setenv("HOME", "/nonexistent", 1) != 0 ||
      setenv("PATH", "/usr/bin:/bin", 1) != 0 ||
      setenv("LANG", "C.UTF-8", 1) != 0 ||
      setenv("LC_ALL", "C.UTF-8", 1) != 0 ||
      setenv("FS2_CAPSULE_LAUNCHER", "fs2-public-edge-capsule-v1", 1) != 0 ||
      setenv("FS2_CAPSULE_GROUP", capsule_group, 1) != 0 ||
      setenv("FS2_CAPSULE_MANIFEST_FD", manifest_text, 1) != 0 ||
      setenv("FS2_CAPSULE_PYTHON_FD", python_text, 1) != 0 ||
      setenv("FS2_CAPSULE_LAUNCHER_FD", launcher_text, 1) != 0 ||
      setenv("FS2_CAPSULE_BOOTSTRAP_FD", bootstrap_text, 1) != 0 ||
      (secret_socket >= 0 &&
       setenv("FS2_CAPSULE_SECRET_BROKER_FD", secret_socket_text, 1) != 0))
    die("cannot establish the fixed capsule environment");
  for (size_t index = 0; gate_environment[index] != NULL; ++index) {
    if (saved[index] != NULL) {
      if (setenv(gate_environment[index], saved[index], 1) != 0)
        die("cannot restore allowed gate environment");
      free(saved[index]);
    }
  }

  static const char *const frozen_entry =
      "import sys;p=sys.argv.pop(1);sys.path[:]=[];b=open(p,'rb',buffering=0).read();"
      "exec(compile(b,'capsule-bootstrap','exec'),{'__name__':'__main__','__file__':p})";
  size_t child_count = (size_t)argc + 10;
  char **child = calloc(child_count, sizeof(char *));
  if (child == NULL) die("cannot allocate Python arguments");
  size_t output = 0;
  child[output++] = python_reference;
  child[output++] = "-I";
  child[output++] = "-S";
  child[output++] = "-B";
  child[output++] = "-c";
  child[output++] = (char *)frozen_entry;
  child[output++] = bootstrap_reference;
  child[output++] = argv[1];
  child[output++] = argv[2];
  for (int index = strcmp(argv[1], "inference-stack") == 0
                       ? forwarded_arguments
                       : 3;
       index < argc; ++index)
    child[output++] = argv[index];
  child[output] = NULL;
  execve(child[0], child, environ);
  die(strerror(errno));
}
