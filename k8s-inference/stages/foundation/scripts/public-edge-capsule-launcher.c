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
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

extern char **environ;

static const char *const launcher_path =
    "/usr/local/libexec/fs2-public-edge-gate-launcher";
static const char *const bootstrap_path =
    "/usr/local/libexec/fs2-public-edge-capsule-bootstrap.py";
static const char *const manifest_path =
    "/usr/local/share/fs2/public-edge-accepted-capsule.json";
static const char *const python_path =
    "/usr/local/libexec/fs2-public-edge-capsule-python3";
static const char *const capsule_group = "fs2-public-edge-capsule";

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
    NULL,
};

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

static int open_protected(const char *path, gid_t group, mode_t mode,
                          int require_group) {
  int descriptor = open(path, O_RDONLY | (require_group ? O_NOFOLLOW : 0));
  if (descriptor < 0) die("cannot open fixed capsule file");
  struct stat details;
  if (fstat(descriptor, &details) != 0 || !S_ISREG(details.st_mode) ||
      details.st_uid != 0 || (require_group && details.st_gid != group) ||
      (require_group && (details.st_mode & 07777) != mode) ||
      (!require_group && (details.st_mode & 0022) != 0))
    die("fixed capsule file is not root-owned with the required protection");
  if (fcntl(descriptor, F_SETFD, 0) != 0)
    die("cannot make protected capsule descriptor inheritable");
  return descriptor;
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

static void require_static_self(gid_t group) {
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
  require_root_directory("/usr/local/share");
  require_root_directory("/usr/local/share/fs2");

  int descriptor = open("/proc/self/exe", O_RDONLY | O_CLOEXEC);
  if (descriptor < 0) die("cannot open launcher executable");
  struct stat details;
  if (fstat(descriptor, &details) != 0 || !S_ISREG(details.st_mode) ||
      details.st_uid != 0 || details.st_gid != group ||
      (details.st_mode & 07777) != 02755)
    die("launcher must be root:capsule mode 2755");

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
      strncmp(name, "FS2_CAPSULE_", 12) == 0)
    return 0;
  return 1;
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
  require_static_self(group);

  const char *operator_names[64] = {0};
  char *operator_values[64] = {0};
  size_t operator_count = 0;
  int forwarded_arguments = 3;
  if (strcmp(argv[1], "inference-stack") == 0) {
    int separator_found = 0;
    for (; forwarded_arguments < argc; ++forwarded_arguments) {
      if (strcmp(argv[forwarded_arguments], "--") == 0) {
        separator_found = 1;
        ++forwarded_arguments;
        break;
      }
      static const char prefix[] = "--preserve-env=";
      if (strncmp(argv[forwarded_arguments], prefix, sizeof(prefix) - 1) != 0 ||
          operator_count >= 64)
        die("operator mode requires bounded --preserve-env=NAME entries then --");
      const char *name = argv[forwarded_arguments] + sizeof(prefix) - 1;
      if (!is_allowed_secret_name(name))
        die("operator environment name is not an allowed secret reference");
      operator_names[operator_count++] = name;
    }
    if (!separator_found) die("operator argument separator is missing");
    for (size_t index = 0; index < operator_count; ++index) {
      const char *value = getenv(operator_names[index]);
      if (value != NULL && (operator_values[index] = strdup(value)) == NULL)
        die("cannot snapshot explicit operator secret environment");
    }
  }

  char *saved[sizeof(gate_environment) / sizeof(gate_environment[0])] = {0};
  if (strcmp(argv[1], "inference-stack") != 0) {
    for (size_t index = 0; gate_environment[index] != NULL; ++index) {
      const char *value = getenv(gate_environment[index]);
      if (value != NULL && (saved[index] = strdup(value)) == NULL)
        die("cannot snapshot allowed gate environment");
    }
  }

  int bootstrap_fd = open_protected(bootstrap_path, group, 0440, 1);
  int manifest_fd = open_protected(manifest_path, group, 0440, 1);
  int python_fd = open_protected(python_path, group, 0550, 1);
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

  if (clearenv() != 0 || setenv("HOME", "/nonexistent", 1) != 0 ||
      setenv("PATH", "/usr/bin:/bin", 1) != 0 ||
      setenv("LANG", "C.UTF-8", 1) != 0 ||
      setenv("LC_ALL", "C.UTF-8", 1) != 0 ||
      setenv("FS2_CAPSULE_LAUNCHER", "fs2-public-edge-capsule-v1", 1) != 0 ||
      setenv("FS2_CAPSULE_GROUP", capsule_group, 1) != 0 ||
      setenv("FS2_CAPSULE_MANIFEST_FD", manifest_text, 1) != 0 ||
      setenv("FS2_CAPSULE_PYTHON_FD", python_text, 1) != 0 ||
      setenv("FS2_CAPSULE_LAUNCHER_FD", launcher_text, 1) != 0 ||
      setenv("FS2_CAPSULE_BOOTSTRAP_FD", bootstrap_text, 1) != 0)
    die("cannot establish the fixed capsule environment");
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
        die("cannot restore allowed gate environment");
      free(saved[index]);
    }
  }

  size_t child_count = (size_t)argc + 7;
  char **child = calloc(child_count, sizeof(char *));
  if (child == NULL) die("cannot allocate Python arguments");
  size_t output = 0;
  child[output++] = python_reference;
  child[output++] = "-I";
  child[output++] = "-B";
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
