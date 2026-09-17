#ifndef FS2_FROZEN_RUNTIME_H
#define FS2_FROZEN_RUNTIME_H

/* Runtime-side proof for the accepted static/frozen Python artifact.
 *
 * The whole static-PIE executable digest is checked by the caller. This verifier also
 * requires four independently reviewed ELF sections: the complete frozen
 * module inventory, the exact concatenated frozen-code payload, reproducible
 * build provenance, and a Platform Security review attestation. The signed
 * artifact digest uses one exact normalization: it hashes the entire ELF in
 * file order while excluding only the attestation section bytes. This avoids
 * a cryptographic fixed point. The signer key is a separately provisioned,
 * root-owned authority outside both the Python artifact and launcher build.
 */

#include <openssl/evp.h>
#include <openssl/crypto.h>

#ifndef DT_RELACOUNT
#define DT_RELACOUNT 0x6ffffff9
#endif
#ifndef DF_1_PIE
#define DF_1_PIE 0x08000000
#endif
#ifndef R_X86_64_IRELATIVE
#define R_X86_64_IRELATIVE 37
#endif
#ifndef DT_DEPAUDIT
#define DT_DEPAUDIT 0x6ffffefb
#endif
#ifndef DT_AUDIT
#define DT_AUDIT 0x6ffffefc
#endif
#ifndef DT_AUXILIARY
#define DT_AUXILIARY 0x7ffffffd
#endif
#ifndef DT_FILTER
#define DT_FILTER 0x7fffffff
#endif

struct fs2_pie_contract {
  Elf64_Rela *relocations;
  size_t count;
};

struct fs2_authority_storage {
  Elf64_Shdr section;
  Elf64_Addr address;
  Elf64_Xword size;
};

static const char *const fs2_review_key_path =
    "/etc/fs2/public-edge-frozen-runtime-review-key.bin";

static uint16_t fs2_u16(const unsigned char *value) {
  return (uint16_t)value[0] | ((uint16_t)value[1] << 8U);
}

static uint32_t fs2_u32(const unsigned char *value) {
  return (uint32_t)value[0] | ((uint32_t)value[1] << 8U) |
         ((uint32_t)value[2] << 16U) | ((uint32_t)value[3] << 24U);
}

static uint64_t fs2_u64(const unsigned char *value) {
  uint64_t result = 0U;
  for (unsigned int index = 0; index < 8U; ++index)
    result |= (uint64_t)value[index] << (index * 8U);
  return result;
}

static int fs2_range_contains(uint64_t outer_start, uint64_t outer_length,
                              uint64_t inner_start, uint64_t inner_length) {
  return inner_start >= outer_start && inner_length <= outer_length &&
         inner_start - outer_start <= outer_length - inner_length;
}

static int fs2_range_disjoint(uint64_t left_start, uint64_t left_length,
                              uint64_t right_start, uint64_t right_length) {
  if (left_start <= right_start)
    return left_length <= right_start - left_start;
  return right_length <= left_start - right_start;
}

static unsigned int fs2_load_membership(
    int descriptor, const Elf64_Ehdr *header, Elf64_Addr address,
    Elf64_Xword length, Elf64_Word required_flags, Elf64_Word forbidden_flags,
    void (*failure)(const char *)) {
  unsigned int matches = 0U;
  for (Elf64_Half index = 0; index < header->e_phnum; ++index) {
    Elf64_Phdr program;
    off_t offset = (off_t)header->e_phoff + (off_t)index * sizeof(program);
    if (pread(descriptor, &program, sizeof(program), offset) !=
        (ssize_t)sizeof(program))
      failure("cannot read frozen-runtime load boundary");
    if (program.p_type == PT_LOAD &&
        fs2_range_contains(program.p_vaddr, program.p_memsz, address, length) &&
        (program.p_flags & required_flags) == required_flags &&
        (program.p_flags & forbidden_flags) == 0U)
      ++matches;
  }
  return matches;
}

static const Elf64_Rela *fs2_relocation_at(
    const struct fs2_pie_contract *contract, Elf64_Addr location) {
  size_t lower = 0U, upper = contract->count;
  while (lower < upper) {
    size_t middle = lower + (upper - lower) / 2U;
    if (contract->relocations[middle].r_offset < location)
      lower = middle + 1U;
    else
      upper = middle;
  }
  if (lower == contract->count ||
      contract->relocations[lower].r_offset != location)
    return NULL;
  return &contract->relocations[lower];
}

static void fs2_require_relative_pointer(
    const struct fs2_pie_contract *contract, Elf64_Addr location,
    Elf64_Addr target, void (*failure)(const char *)) {
  const Elf64_Rela *relocation = fs2_relocation_at(contract, location);
  if (relocation == NULL || ELF64_R_SYM(relocation->r_info) != 0U ||
      ELF64_R_TYPE(relocation->r_info) != R_X86_64_RELATIVE ||
      relocation->r_addend < 0 || (Elf64_Addr)relocation->r_addend != target)
    failure("static-PIE pointer lacks its exact bounded relative relocation");
}

static void fs2_require_no_relocation(
    const struct fs2_pie_contract *contract, Elf64_Addr location,
    void (*failure)(const char *)) {
  if (fs2_relocation_at(contract, location) != NULL)
    failure("static-PIE relocation targets a non-pointer frozen-table field");
}

static void fs2_hash_range(int descriptor, Elf64_Off start, Elf64_Xword length,
                           unsigned char result[32],
                           void (*failure)(const char *)) {
  struct fs2_sha256 context;
  fs2_sha256_init(&context);
  unsigned char buffer[32768];
  Elf64_Xword offset = 0U;
  while (offset < length) {
    size_t wanted = sizeof(buffer);
    if ((Elf64_Xword)wanted > length - offset)
      wanted = (size_t)(length - offset);
    ssize_t count = pread(descriptor, buffer, wanted,
                          (off_t)start + (off_t)offset);
    if (count <= 0 || (size_t)count != wanted)
      failure("cannot read frozen-runtime artifact range");
    fs2_sha256_update(&context, buffer, wanted);
    offset += (Elf64_Xword)wanted;
  }
  fs2_sha256_final(&context, result);
}

static void fs2_require_symbol(int descriptor, const Elf64_Shdr *symbols,
                               const Elf64_Shdr *strings, const char *name,
                               Elf64_Addr value, Elf64_Xword size,
                               void (*failure)(const char *)) {
  if (symbols->sh_entsize != sizeof(Elf64_Sym) || strings->sh_size == 0U ||
      strings->sh_size > 16U * 1024U * 1024U)
    failure("frozen-runtime symbol table is unsupported");
  char *names = calloc((size_t)strings->sh_size + 1U, 1U);
  if (names == NULL ||
      pread(descriptor, names, (size_t)strings->sh_size,
            (off_t)strings->sh_offset) != (ssize_t)strings->sh_size)
    failure("cannot read frozen-runtime symbol names");
  unsigned int matches = 0U;
  for (Elf64_Xword offset = 0U; offset < symbols->sh_size;
       offset += symbols->sh_entsize) {
    Elf64_Sym symbol;
    if (pread(descriptor, &symbol, sizeof(symbol),
              (off_t)symbols->sh_offset + (off_t)offset) !=
            (ssize_t)sizeof(symbol) ||
        symbol.st_name >= strings->sh_size)
      failure("cannot read frozen-runtime symbol table");
    const char *candidate = names + symbol.st_name;
    if (memchr(candidate, '\0', (size_t)strings->sh_size - symbol.st_name) ==
        NULL)
      failure("frozen-runtime symbol name is unterminated");
    if (strcmp(candidate, name) == 0) {
      ++matches;
      if (symbol.st_value != value || (size != 0U && symbol.st_size != size) ||
          ELF64_ST_BIND(symbol.st_info) != STB_GLOBAL)
        failure("frozen-runtime linkage symbol has an unexpected extent");
    }
  }
  free(names);
  if (matches != 1U)
    failure("frozen-runtime linkage symbol is absent or ambiguous");
}

static int fs2_module_name(const unsigned char *name, uint16_t length) {
  if (length == 0U || length > 255U) return 0;
  for (uint16_t index = 0; index < length; ++index) {
    unsigned char value = name[index];
    if (!((value >= 'A' && value <= 'Z') ||
          (value >= 'a' && value <= 'z') ||
          (value >= '0' && value <= '9') || value == '_' || value == '.'))
      return 0;
  }
  return 1;
}

static int fs2_has_module(const unsigned char *inventory, size_t length,
                          const char *wanted) {
  uint32_t count = fs2_u32(inventory + 8U);
  size_t cursor = 16U;
  for (uint32_t index = 0U; index < count && cursor + 52U <= length; ++index) {
    uint16_t name_length = fs2_u16(inventory + cursor);
    cursor += 52U;
    if (cursor + name_length + 1U > length ||
        inventory[cursor + name_length] != 0U)
      return 0;
    if (strlen(wanted) == name_length &&
        memcmp(inventory + cursor, wanted, name_length) == 0)
      return 1;
    cursor += name_length + 1U;
  }
  return 0;
}

static void fs2_verify_inventory(int descriptor, const Elf64_Shdr *inventory,
                                 const Elf64_Shdr *payload,
                                 void (*failure)(const char *)) {
  if (inventory->sh_size < 16U || inventory->sh_size > 4U * 1024U * 1024U ||
      payload->sh_size == 0U || payload->sh_size > 256U * 1024U * 1024U)
    failure("frozen-module closure exceeds its exact bounds");
  unsigned char *raw = malloc((size_t)inventory->sh_size);
  if (raw == NULL ||
      pread(descriptor, raw, (size_t)inventory->sh_size,
            (off_t)inventory->sh_offset) != (ssize_t)inventory->sh_size)
    failure("cannot read frozen-module inventory");
  if (memcmp(raw, "FS2FRZ1\0", 8U) != 0 || fs2_u32(raw + 12U) != 0U)
    failure("frozen-module inventory header is unsupported");
  uint32_t count = fs2_u32(raw + 8U);
  if (count < 16U || count > 65535U)
    failure("frozen-module inventory count is outside its bounds");
  size_t cursor = 16U;
  uint64_t payload_cursor = 0U;
  const unsigned char *prior_name = NULL;
  uint16_t prior_length = 0U;
  for (uint32_t index = 0U; index < count; ++index) {
    if (cursor + 52U > inventory->sh_size)
      failure("frozen-module inventory is truncated");
    uint16_t name_length = fs2_u16(raw + cursor);
    unsigned char flags = raw[cursor + 2U];
    uint64_t offset = fs2_u64(raw + cursor + 4U);
    uint64_t module_length = fs2_u64(raw + cursor + 12U);
    const unsigned char *expected_digest = raw + cursor + 20U;
    cursor += 52U;
    if ((flags & ~1U) != 0U || raw[cursor - 49U] != 0U ||
        cursor + name_length + 1U > inventory->sh_size ||
        !fs2_module_name(raw + cursor, name_length) ||
        raw[cursor + name_length] != 0U ||
        offset != payload_cursor || offset > payload->sh_size ||
        module_length == 0U || module_length > payload->sh_size - offset)
      failure("frozen-module inventory record is malformed or non-contiguous");
    if (prior_name != NULL) {
      size_t shorter = prior_length < name_length ? prior_length : name_length;
      int order = memcmp(prior_name, raw + cursor, shorter);
      if (order > 0 || (order == 0 && prior_length >= name_length))
        failure("frozen-module names are not strictly sorted and unique");
    }
    unsigned char observed_digest[32];
    fs2_hash_range(descriptor, payload->sh_offset + offset, module_length,
                   observed_digest, failure);
    if (memcmp(observed_digest, expected_digest, 32U) != 0)
      failure("frozen-module payload differs from its complete inventory");
    prior_name = raw + cursor;
    prior_length = name_length;
    cursor += name_length + 1U;
    payload_cursor += module_length;
  }
  if (cursor != inventory->sh_size || payload_cursor != payload->sh_size)
    failure("frozen-module inventory has gaps or unreviewed trailing payload");
  static const char *const mandatory[] = {
      "__main__", "_frozen_importlib", "_frozen_importlib_external",
      "argparse", "base64", "codecs", "encodings", "collections.abc",
      "contextlib", "ctypes", "datetime", "hashlib", "http.client",
      "http.server", "importlib", "json", "os", "pathlib", "re", "signal",
      "socket", "stat", "struct", "subprocess", "tempfile", "threading",
      "types", "typing",
      "urllib.error", "urllib.parse", "urllib.request", "uuid", NULL};
  for (size_t index = 0; mandatory[index] != NULL; ++index)
    if (!fs2_has_module(raw, (size_t)inventory->sh_size, mandatory[index]))
      failure("frozen-module inventory omits a launcher-required module");
  free(raw);
}

static void fs2_verify_cpython_table(int descriptor,
                                     const Elf64_Shdr *inventory,
                                     const Elf64_Shdr *payload,
                                     const Elf64_Shdr *table,
                                     const struct fs2_pie_contract *contract,
                                     void (*failure)(const char *)) {
  unsigned char header[16];
  if (pread(descriptor, header, sizeof(header), (off_t)inventory->sh_offset) !=
      (ssize_t)sizeof(header))
    failure("cannot read frozen inventory for CPython table verification");
  uint32_t count = fs2_u32(header + 8U);
  if (table->sh_size != ((Elf64_Xword)count + 1U) * 32U)
    failure("actual CPython _frozen table has the wrong exact row count");
  size_t cursor = 16U;
  for (uint32_t index = 0U; index < count; ++index) {
    unsigned char record[52], row[32];
    if (pread(descriptor, record, sizeof(record),
              (off_t)inventory->sh_offset + (off_t)cursor) !=
            (ssize_t)sizeof(record) ||
        pread(descriptor, row, sizeof(row),
              (off_t)table->sh_offset + (off_t)index * 32) !=
            (ssize_t)sizeof(row))
      failure("cannot read actual CPython frozen table row");
    uint16_t name_length = fs2_u16(record);
    uint64_t payload_offset = fs2_u64(record + 4U);
    uint64_t payload_length = fs2_u64(record + 12U);
    uint64_t expected_name = inventory->sh_addr + cursor + 52U;
    uint64_t expected_code = payload->sh_addr + payload_offset;
    fs2_require_relative_pointer(contract, table->sh_addr + index * 32U,
                                 expected_name, failure);
    fs2_require_relative_pointer(contract,
                                 table->sh_addr + index * 32U + 8U,
                                 expected_code, failure);
    fs2_require_no_relocation(contract,
                              table->sh_addr + index * 32U + 16U, failure);
    fs2_require_no_relocation(contract,
                              table->sh_addr + index * 32U + 20U, failure);
    fs2_require_no_relocation(contract,
                              table->sh_addr + index * 32U + 24U, failure);
    if (fs2_u32(row + 16U) != payload_length ||
        fs2_u32(row + 20U) != (uint32_t)(record[2U] & 1U) ||
        fs2_u64(row + 24U) != 0U || payload_length > INT32_MAX)
      failure("actual CPython _frozen row differs from signed inventory/payload");
    cursor += 52U + name_length + 1U;
  }
  unsigned char sentinel[32];
  if (pread(descriptor, sentinel, sizeof(sentinel),
            (off_t)table->sh_offset + (off_t)count * 32) !=
          (ssize_t)sizeof(sentinel))
    failure("cannot read CPython frozen-table sentinel");
  for (size_t index = 0U; index < sizeof(sentinel); ++index)
    if (sentinel[index] != 0U)
      failure("actual CPython frozen table lacks an all-zero terminal row");
  for (size_t index = 0U; index < sizeof(sentinel); index += 8U)
    fs2_require_no_relocation(contract,
                              table->sh_addr + (Elf64_Addr)count * 32U + index,
                              failure);
  for (size_t index = 0U; index < contract->count; ++index) {
    Elf64_Addr location = contract->relocations[index].r_offset;
    if (!fs2_range_disjoint(location, 8U, table->sh_addr, table->sh_size)) {
      Elf64_Addr relative = location - table->sh_addr;
      if (relative / 32U >= count ||
          !(relative % 32U == 0U || relative % 32U == 8U))
        failure("unexpected static-PIE relocation overlaps the CPython frozen table");
    }
  }
}

static void fs2_require_pointer_symbol(
    int descriptor, const Elf64_Ehdr *header, const Elf64_Shdr *symbols,
    const Elf64_Shdr *strings, const char *name, Elf64_Addr target,
    const struct fs2_pie_contract *contract,
    struct fs2_authority_storage *pointer_storage,
    void (*failure)(const char *)) {
  char *names = calloc((size_t)strings->sh_size + 1U, 1U);
  if (names == NULL ||
      pread(descriptor, names, (size_t)strings->sh_size,
            (off_t)strings->sh_offset) != (ssize_t)strings->sh_size)
    failure("cannot read CPython pointer symbol names");
  unsigned int matches = 0U;
  for (Elf64_Xword offset = 0U; offset < symbols->sh_size;
       offset += symbols->sh_entsize) {
    Elf64_Sym symbol;
    if (pread(descriptor, &symbol, sizeof(symbol),
              (off_t)symbols->sh_offset + (off_t)offset) !=
            (ssize_t)sizeof(symbol) ||
        symbol.st_name >= strings->sh_size)
      failure("cannot read CPython pointer symbol");
    const char *candidate = names + symbol.st_name;
    if (memchr(candidate, '\0', (size_t)strings->sh_size - symbol.st_name) ==
        NULL)
      failure("CPython pointer symbol name is unterminated");
    if (strcmp(candidate, name) == 0) {
      ++matches;
      if (ELF64_ST_BIND(symbol.st_info) != STB_GLOBAL || symbol.st_size != 8U ||
          symbol.st_shndx == SHN_UNDEF || symbol.st_shndx >= header->e_shnum)
        failure("CPython pointer symbol is not one defined global pointer");
      Elf64_Shdr storage;
      off_t section_offset = (off_t)header->e_shoff +
                             (off_t)symbol.st_shndx * sizeof(storage);
      if (pread(descriptor, &storage, sizeof(storage), section_offset) !=
              (ssize_t)sizeof(storage) ||
          symbol.st_value < storage.sh_addr ||
          symbol.st_value + 8U > storage.sh_addr + storage.sh_size)
        failure("CPython pointer storage is outside its ELF section");
      fs2_require_relative_pointer(contract, symbol.st_value, target, failure);
      for (size_t relocation = 0U; relocation < contract->count; ++relocation)
        if (!fs2_range_disjoint(contract->relocations[relocation].r_offset, 8U,
                                symbol.st_value, 8U) &&
            contract->relocations[relocation].r_offset != symbol.st_value)
          failure("unexpected relocation overlaps a CPython pointer variable");
      pointer_storage->section = storage;
      pointer_storage->address = symbol.st_value;
      pointer_storage->size = symbol.st_size;
    }
  }
  free(names);
  if (matches != 1U)
    failure("CPython pointer variable is absent or ambiguous");
}

static void fs2_require_inittab_symbol(
    int descriptor, const Elf64_Ehdr *header, const Elf64_Shdr *symbols,
    const Elf64_Shdr *strings, Elf64_Sym *inittab,
    Elf64_Shdr *inittab_storage, void (*failure)(const char *)) {
  char *names = calloc((size_t)strings->sh_size + 1U, 1U);
  if (names == NULL ||
      pread(descriptor, names, (size_t)strings->sh_size,
            (off_t)strings->sh_offset) != (ssize_t)strings->sh_size)
    failure("cannot read PyImport_Inittab symbol names");
  unsigned int matches = 0U;
  for (Elf64_Xword offset = 0U; offset < symbols->sh_size;
       offset += symbols->sh_entsize) {
    Elf64_Sym symbol;
    if (pread(descriptor, &symbol, sizeof(symbol),
              (off_t)symbols->sh_offset + (off_t)offset) !=
            (ssize_t)sizeof(symbol) ||
        symbol.st_name >= strings->sh_size)
      failure("cannot read PyImport_Inittab symbol");
    const char *candidate = names + symbol.st_name;
    if (memchr(candidate, '\0', (size_t)strings->sh_size - symbol.st_name) ==
        NULL)
      failure("PyImport_Inittab symbol name is unterminated");
    if (strcmp(candidate, "PyImport_Inittab") == 0) {
      ++matches;
      if (ELF64_ST_BIND(symbol.st_info) != STB_GLOBAL ||
          symbol.st_shndx == SHN_UNDEF || symbol.st_shndx >= header->e_shnum ||
          symbol.st_size < 16U || symbol.st_size > 512U * 16U ||
          symbol.st_size % 16U != 0U)
        failure("PyImport_Inittab is not one bounded direct _inittab array");
      off_t section_offset = (off_t)header->e_shoff +
                             (off_t)symbol.st_shndx * sizeof(*inittab_storage);
      if (pread(descriptor, inittab_storage, sizeof(*inittab_storage),
                section_offset) != (ssize_t)sizeof(*inittab_storage) ||
          inittab_storage->sh_type != SHT_PROGBITS ||
          !fs2_range_contains(inittab_storage->sh_addr,
                              inittab_storage->sh_size, symbol.st_value,
                              symbol.st_size))
        failure("PyImport_Inittab is outside its file-backed ELF section");
      *inittab = symbol;
    }
  }
  free(names);
  if (matches != 1U)
    failure("PyImport_Inittab is absent or ambiguous");
}

static void fs2_pread_virtual(int descriptor, const Elf64_Ehdr *header,
                              Elf64_Addr address, void *buffer,
                              size_t length, void (*failure)(const char *)) {
  unsigned int matches = 0U;
  for (Elf64_Half index = 0; index < header->e_phnum; ++index) {
    Elf64_Phdr program;
    off_t offset = (off_t)header->e_phoff + (off_t)index * sizeof(program);
    if (pread(descriptor, &program, sizeof(program), offset) !=
        (ssize_t)sizeof(program))
      failure("cannot read CPython table load mapping");
    if (program.p_type == PT_LOAD &&
        fs2_range_contains(program.p_vaddr, program.p_filesz, address,
                           (Elf64_Xword)length)) {
      Elf64_Off file_offset = program.p_offset + address - program.p_vaddr;
      if (pread(descriptor, buffer, length, (off_t)file_offset) !=
          (ssize_t)length)
        failure("cannot read CPython table mapped bytes");
      ++matches;
    }
  }
  if (matches != 1U)
    failure("CPython table address lacks one exact file-backed PT_LOAD mapping");
}

static void fs2_verify_inittab(
    int descriptor, const Elf64_Ehdr *header, const Elf64_Sym *inittab,
    const Elf64_Shdr *inventory, const struct fs2_pie_contract *contract,
    void (*failure)(const char *)) {
  static const char *const mandatory[] = {
      "_ctypes", "_hashlib", "_imp", "_io", "_socket", "_thread",
      "_warnings", "_weakref", "array", "atexit", "binascii", "builtins",
      "errno", "faulthandler", "fcntl", "grp", "marshal", "math", "posix",
      "pwd", "select", "sys", "time", NULL};
  unsigned char found[23] = {0};
  unsigned char *inventory_raw = malloc((size_t)inventory->sh_size);
  if (inventory_raw == NULL ||
      pread(descriptor, inventory_raw, (size_t)inventory->sh_size,
            (off_t)inventory->sh_offset) != (ssize_t)inventory->sh_size)
    failure("cannot read inventory for builtin/frozen disjointness");
  size_t rows = (size_t)(inittab->st_size / 16U);
  for (size_t index = 0U; index < rows; ++index) {
    Elf64_Addr row = inittab->st_value + (Elf64_Addr)index * 16U;
    unsigned char bytes[16];
    fs2_pread_virtual(descriptor, header, row, bytes, sizeof(bytes), failure);
    const Elf64_Rela *name = fs2_relocation_at(contract, row);
    const Elf64_Rela *function = fs2_relocation_at(contract, row + 8U);
    if (index == rows - 1U) {
      for (size_t byte = 0U; byte < sizeof(bytes); ++byte)
        if (bytes[byte] != 0U)
          failure("PyImport_Inittab terminal row is not all zero");
      if (name != NULL || function != NULL)
        failure("PyImport_Inittab terminal row is relocation-targeted");
      continue;
    }
    if (name == NULL || ELF64_R_SYM(name->r_info) != 0U ||
        ELF64_R_TYPE(name->r_info) != R_X86_64_RELATIVE ||
        name->r_addend < 0 || function == NULL ||
        ELF64_R_SYM(function->r_info) != 0U ||
        !(ELF64_R_TYPE(function->r_info) == R_X86_64_RELATIVE ||
          ELF64_R_TYPE(function->r_info) == R_X86_64_IRELATIVE) ||
        function->r_addend < 0 ||
        fs2_load_membership(descriptor, header,
                            (Elf64_Addr)function->r_addend, 1U,
                            PF_R | PF_X, PF_W, failure) != 1U)
      failure("PyImport_Inittab row is not bound to reviewed name/code mappings");
    char module[256] = {0};
    size_t length = 0U;
    for (; length < sizeof(module) - 1U; ++length) {
      fs2_pread_virtual(descriptor, header,
                        (Elf64_Addr)name->r_addend + length,
                        &module[length], 1U, failure);
      if (module[length] == '\0') break;
    }
    if (length == 0U || length == sizeof(module) - 1U)
      failure("PyImport_Inittab module name is empty or unterminated");
    for (size_t character = 0U; character < length; ++character)
      if (!((module[character] >= 'A' && module[character] <= 'Z') ||
            (module[character] >= 'a' && module[character] <= 'z') ||
            (module[character] >= '0' && module[character] <= '9') ||
            module[character] == '_' || module[character] == '.'))
        failure("PyImport_Inittab module name is malformed");
    if (fs2_has_module(inventory_raw, (size_t)inventory->sh_size, module))
      failure("a module is ambiguously executable as frozen and builtin code");
    for (size_t prior = 0U; prior < index; ++prior) {
      Elf64_Addr prior_row = inittab->st_value + (Elf64_Addr)prior * 16U;
      const Elf64_Rela *prior_name = fs2_relocation_at(contract, prior_row);
      char prior_module[256] = {0};
      size_t prior_length = 0U;
      if (prior_name == NULL || prior_name->r_addend < 0)
        failure("prior PyImport_Inittab name relocation is malformed");
      for (; prior_length < sizeof(prior_module) - 1U; ++prior_length) {
        fs2_pread_virtual(descriptor, header,
                          (Elf64_Addr)prior_name->r_addend + prior_length,
                          &prior_module[prior_length], 1U, failure);
        if (prior_module[prior_length] == '\0') break;
      }
      if (strcmp(module, prior_module) == 0)
        failure("PyImport_Inittab contains a duplicate module name");
    }
    for (size_t required = 0U; mandatory[required] != NULL; ++required)
      if (strcmp(module, mandatory[required]) == 0)
        found[required] = 1U;
  }
  for (size_t required = 0U; mandatory[required] != NULL; ++required)
    if (found[required] == 0U)
      failure("PyImport_Inittab omits a required native module");
  free(inventory_raw);
}

static void fs2_require_readonly_mapping(int descriptor,
                                         const Elf64_Ehdr *header,
                                         const Elf64_Shdr *section,
                                         int allow_relro,
                                         void (*failure)(const char *)) {
  if (!(section->sh_flags == SHF_ALLOC ||
        (allow_relro && section->sh_flags == (SHF_ALLOC | SHF_WRITE))) ||
      section->sh_size == 0U)
    failure("frozen runtime section is not read-only ALLOC PROGBITS");
  unsigned int mappings = 0U;
  for (Elf64_Half index = 0; index < header->e_phnum; ++index) {
    Elf64_Phdr program;
    off_t offset = (off_t)header->e_phoff + (off_t)index * sizeof(program);
    if (pread(descriptor, &program, sizeof(program), offset) !=
        (ssize_t)sizeof(program))
      failure("cannot read frozen-runtime PT_LOAD mapping");
    if (program.p_type != PT_LOAD) continue;
    int file_contains = fs2_range_contains(
        program.p_offset, program.p_filesz, section->sh_offset,
        section->sh_size);
    int virtual_contains = fs2_range_contains(
        program.p_vaddr, program.p_memsz, section->sh_addr, section->sh_size);
    if (file_contains || virtual_contains) {
      int protected_after_relocation = 0;
      if (allow_relro && program.p_flags == (PF_R | PF_W)) {
        for (Elf64_Half relro_index = 0; relro_index < header->e_phnum;
             ++relro_index) {
          Elf64_Phdr relro;
          off_t relro_offset = (off_t)header->e_phoff +
                               (off_t)relro_index * sizeof(relro);
          if (pread(descriptor, &relro, sizeof(relro), relro_offset) !=
              (ssize_t)sizeof(relro))
            failure("cannot read frozen-runtime RELRO boundary");
          if (relro.p_type == PT_GNU_RELRO &&
              fs2_range_contains(relro.p_vaddr, relro.p_memsz,
                                 section->sh_addr, section->sh_size))
            ++protected_after_relocation;
        }
      }
      if (!file_contains || !virtual_contains ||
          !(program.p_flags == PF_R || protected_after_relocation == 1) ||
          section->sh_offset - program.p_offset !=
              section->sh_addr - program.p_vaddr)
        failure("frozen runtime file bytes and runtime-read-only mapping are not congruent");
      ++mappings;
    }
  }
  if (mappings != 1U)
    failure("frozen runtime section is not covered by one exact read-only PT_LOAD");
}

static void fs2_require_disjoint_sections(const Elf64_Shdr *left,
                                          const Elf64_Shdr *right,
                                          void (*failure)(const char *)) {
  int file_disjoint = fs2_range_disjoint(
      left->sh_offset, left->sh_size, right->sh_offset, right->sh_size);
  int virtual_disjoint = fs2_range_disjoint(
      left->sh_addr, left->sh_size, right->sh_addr, right->sh_size);
  if (!file_disjoint || !virtual_disjoint)
    failure("frozen runtime authority sections overlap in file or virtual memory");
}

static int fs2_same_section(const Elf64_Shdr *left, const Elf64_Shdr *right) {
  return memcmp(left, right, sizeof(*left)) == 0;
}

static Elf64_Off fs2_authority_file_offset(
    const struct fs2_authority_storage *authority,
    void (*failure)(const char *)) {
  if (!fs2_range_contains(authority->section.sh_addr,
                          authority->section.sh_size, authority->address,
                          authority->size))
    failure("CPython authority object is outside its storage section");
  return authority->section.sh_offset +
         (authority->address - authority->section.sh_addr);
}

static void fs2_require_authority_storage_disjoint(
    const Elf64_Shdr *const *core, size_t core_count,
    const struct fs2_authority_storage *authorities, size_t authority_count,
    void (*failure)(const char *)) {
  for (size_t index = 0U; index < authority_count; ++index) {
    const struct fs2_authority_storage *authority = &authorities[index];
    Elf64_Off file_offset = fs2_authority_file_offset(authority, failure);
    if (authority->size == 0U)
      failure("CPython authority object has an empty exact range");
    for (size_t prior = 0U; prior < index; ++prior) {
      const struct fs2_authority_storage *other = &authorities[prior];
      Elf64_Off other_file = fs2_authority_file_offset(other, failure);
      if (!fs2_range_disjoint(file_offset, authority->size, other_file,
                              other->size) ||
          !fs2_range_disjoint(authority->address, authority->size,
                              other->address, other->size))
        failure("CPython authority storage objects overlap");
      if (!fs2_same_section(&authority->section, &other->section))
        fs2_require_disjoint_sections(&authority->section, &other->section,
                                      failure);
    }
    for (size_t core_index = 0U; core_index < core_count; ++core_index) {
      const Elf64_Shdr *section = core[core_index];
      fs2_require_disjoint_sections(&authority->section, section, failure);
      if (!fs2_range_disjoint(file_offset, authority->size,
                              section->sh_offset, section->sh_size) ||
          !fs2_range_disjoint(authority->address, authority->size,
                              section->sh_addr, section->sh_size))
        failure("CPython authority object overlaps reviewed frozen data");
    }
  }
}

static void fs2_verify_static_pie(
    int descriptor, const Elf64_Ehdr *header, const Elf64_Shdr *dynamic,
    const Elf64_Shdr *rela, struct fs2_pie_contract *contract,
    void (*failure)(const char *)) {
  if (dynamic->sh_type != SHT_DYNAMIC ||
      dynamic->sh_entsize != sizeof(Elf64_Dyn) || dynamic->sh_size == 0U ||
      dynamic->sh_size % sizeof(Elf64_Dyn) != 0U ||
      dynamic->sh_size > 1024U * 1024U || rela->sh_type != SHT_RELA ||
      rela->sh_entsize != sizeof(Elf64_Rela) || rela->sh_size == 0U ||
      rela->sh_size % sizeof(Elf64_Rela) != 0U ||
      rela->sh_size > 64U * 1024U * 1024U)
    failure("static-PIE dynamic or relocation section is not exactly bounded");
  fs2_require_readonly_mapping(descriptor, header, dynamic, 1, failure);
  fs2_require_readonly_mapping(descriptor, header, rela, 0, failure);

  unsigned int dynamic_segments = 0U;
  for (Elf64_Half index = 0; index < header->e_phnum; ++index) {
    Elf64_Phdr program;
    off_t offset = (off_t)header->e_phoff + (off_t)index * sizeof(program);
    if (pread(descriptor, &program, sizeof(program), offset) !=
        (ssize_t)sizeof(program))
      failure("cannot read static-PIE dynamic program header");
    if (program.p_type == PT_INTERP)
      failure("static-PIE frozen Python must not have an external interpreter");
    if (program.p_type == PT_DYNAMIC) {
      ++dynamic_segments;
      if (program.p_offset != dynamic->sh_offset ||
          program.p_vaddr != dynamic->sh_addr ||
          program.p_filesz != dynamic->sh_size ||
          program.p_memsz != dynamic->sh_size)
        failure("PT_DYNAMIC does not exactly describe the reviewed dynamic section");
    }
  }
  if (dynamic_segments != 1U)
    failure("static-PIE frozen Python must have one exact PT_DYNAMIC segment");

  Elf64_Addr rela_address = 0U;
  Elf64_Xword rela_size = 0U, rela_entry = 0U, relative_count = 0U;
  Elf64_Xword flags = 0U, flags_1 = 0U;
  unsigned int seen_rela = 0U, seen_rela_size = 0U, seen_rela_entry = 0U;
  unsigned int seen_relative_count = 0U, seen_flags = 0U, seen_flags_1 = 0U;
  int terminated = 0;
  for (Elf64_Xword offset = 0U; offset < dynamic->sh_size;
       offset += sizeof(Elf64_Dyn)) {
    Elf64_Dyn entry;
    if (pread(descriptor, &entry, sizeof(entry),
              (off_t)dynamic->sh_offset + (off_t)offset) !=
        (ssize_t)sizeof(entry))
      failure("cannot read static-PIE dynamic contract");
    if (terminated) {
      if (entry.d_tag != DT_NULL || entry.d_un.d_val != 0U)
        failure("static-PIE dynamic table has data after its terminator");
      continue;
    }
    if (entry.d_tag == DT_NULL) {
      terminated = 1;
      continue;
    }
    switch (entry.d_tag) {
      case DT_RELA:
        if (++seen_rela != 1U) failure("duplicate DT_RELA");
        rela_address = entry.d_un.d_ptr;
        break;
      case DT_RELASZ:
        if (++seen_rela_size != 1U) failure("duplicate DT_RELASZ");
        rela_size = entry.d_un.d_val;
        break;
      case DT_RELAENT:
        if (++seen_rela_entry != 1U) failure("duplicate DT_RELAENT");
        rela_entry = entry.d_un.d_val;
        break;
      case DT_RELACOUNT:
        if (++seen_relative_count != 1U) failure("duplicate DT_RELACOUNT");
        relative_count = entry.d_un.d_val;
        break;
      case DT_FLAGS:
        if (++seen_flags != 1U) failure("duplicate DT_FLAGS");
        flags = entry.d_un.d_val;
        break;
      case DT_FLAGS_1:
        if (++seen_flags_1 != 1U) failure("duplicate DT_FLAGS_1");
        flags_1 = entry.d_un.d_val;
        break;
      case DT_NEEDED:
      case DT_SONAME:
      case DT_RPATH:
      case DT_RUNPATH:
      case DT_DEPAUDIT:
      case DT_AUDIT:
      case DT_AUXILIARY:
      case DT_FILTER:
      case DT_JMPREL:
      case DT_PLTRELSZ:
      case DT_PLTREL:
      case DT_REL:
      case DT_RELSZ:
      case DT_RELENT:
      case DT_TEXTREL:
        failure("static-PIE frozen Python declares an external or non-RELA dependency");
        break;
      default:
        break;
    }
  }
  if (!terminated || seen_rela != 1U || seen_rela_size != 1U ||
      seen_rela_entry != 1U || seen_relative_count != 1U ||
      seen_flags_1 != 1U || rela_address != rela->sh_addr ||
      rela_size != rela->sh_size || rela_entry != sizeof(Elf64_Rela) ||
      relative_count > rela_size / sizeof(Elf64_Rela) ||
      (flags & DF_TEXTREL) != 0U || (flags_1 & DF_1_PIE) == 0U)
    failure("static-PIE dynamic contract does not bind exact self-relocation");

  contract->count = (size_t)(rela->sh_size / sizeof(Elf64_Rela));
  contract->relocations = calloc(contract->count, sizeof(Elf64_Rela));
  if (contract->relocations == NULL ||
      pread(descriptor, contract->relocations, (size_t)rela->sh_size,
            (off_t)rela->sh_offset) != (ssize_t)rela->sh_size)
    failure("cannot read static-PIE relocation closure");
  Elf64_Addr prior_target = 0U;
  for (size_t index = 0U; index < contract->count; ++index) {
    const Elf64_Rela *entry = &contract->relocations[index];
    Elf64_Xword type = ELF64_R_TYPE(entry->r_info);
    if (ELF64_R_SYM(entry->r_info) != 0U ||
        (index != 0U && entry->r_offset <= prior_target) ||
        fs2_load_membership(descriptor, header, entry->r_offset, 8U,
                            PF_R | PF_W, PF_X, failure) != 1U ||
        entry->r_addend < 0)
      failure("static-PIE relocation target is ambiguous or outside writable data");
    if (index < relative_count) {
      if (type != R_X86_64_RELATIVE ||
          fs2_load_membership(descriptor, header,
                              (Elf64_Addr)entry->r_addend, 1U, PF_R, 0U,
                              failure) != 1U)
        failure("static-PIE relative relocation escapes the reviewed image");
    } else if (type != R_X86_64_IRELATIVE ||
               fs2_load_membership(descriptor, header,
                                   (Elf64_Addr)entry->r_addend, 1U,
                                   PF_R | PF_X, PF_W, failure) != 1U) {
      failure("static-PIE resolver relocation is not reviewed executable code");
    }
    prior_target = entry->r_offset;
  }
}

static int fs2_review_key(void (*failure)(const char *), unsigned char key[32]) {
  struct stat directory;
  if (lstat("/etc", &directory) != 0 || !S_ISDIR(directory.st_mode) ||
      directory.st_uid != 0 || (directory.st_mode & 0022) != 0 ||
      lstat("/etc/fs2", &directory) != 0 || !S_ISDIR(directory.st_mode) ||
      directory.st_uid != 0 || (directory.st_mode & 0022) != 0)
    failure("frozen-runtime reviewer trust directory is not protected");
  int descriptor = open(fs2_review_key_path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  struct stat details;
  if (descriptor < 0 || fstat(descriptor, &details) != 0 ||
      !S_ISREG(details.st_mode) || details.st_uid != 0 || details.st_gid != 0 ||
      (details.st_mode & 07777) != 0444 || details.st_size != 32 ||
      pread(descriptor, key, 32U, 0) != 32)
    failure("frozen-runtime reviewer key is not the external protected authority");
  close(descriptor);
  return 1;
}

static void fs2_verify_attestation(int descriptor, const Elf64_Shdr *attestation,
                                   const Elf64_Shdr *inventory,
                                   const Elf64_Shdr *payload,
                                   const Elf64_Shdr *provenance,
                                   void (*failure)(const char *)) {
  /* FS2ATT1\0, version, reviewer-key SHA-256, artifact SHA-256 excluding this
   * section, inventory/payload/provenance SHA-256, Ed25519 signature. */
  if (attestation->sh_size != 236U)
    failure("frozen-runtime review attestation has the wrong exact size");
  unsigned char raw[236];
  if (pread(descriptor, raw, sizeof(raw), (off_t)attestation->sh_offset) !=
          (ssize_t)sizeof(raw) ||
      memcmp(raw, "FS2ATT1\0", 8U) != 0 || fs2_u32(raw + 8U) != 1U)
    failure("frozen-runtime review attestation has an unsupported format");
  unsigned char public_key[32], key_digest[32], inventory_digest[32];
  unsigned char payload_digest[32], provenance_digest[32];
  (void)fs2_review_key(failure, public_key);
  struct fs2_sha256 key_context;
  fs2_sha256_init(&key_context);
  fs2_sha256_update(&key_context, public_key, sizeof(public_key));
  fs2_sha256_final(&key_context, key_digest);
  if (memcmp(key_digest, raw + 12U, 32U) != 0)
    failure("frozen-runtime review attestation names a different trust key");
  fs2_hash_range(descriptor, inventory->sh_offset, inventory->sh_size,
                 inventory_digest, failure);
  fs2_hash_range(descriptor, payload->sh_offset, payload->sh_size,
                 payload_digest, failure);
  fs2_hash_range(descriptor, provenance->sh_offset, provenance->sh_size,
                 provenance_digest, failure);
  if (memcmp(inventory_digest, raw + 76U, 32U) != 0 ||
      memcmp(payload_digest, raw + 108U, 32U) != 0 ||
      memcmp(provenance_digest, raw + 140U, 32U) != 0)
    failure("signed frozen-runtime closure digests differ from the ELF sections");
  struct stat details;
  if (fstat(descriptor, &details) != 0 || details.st_size <= 0 ||
      (Elf64_Off)details.st_size < attestation->sh_offset + attestation->sh_size)
    failure("cannot bound the frozen-runtime artifact digest");
  struct fs2_sha256 artifact_context;
  fs2_sha256_init(&artifact_context);
  unsigned char buffer[32768];
  for (off_t offset = 0; offset < details.st_size;) {
    if ((Elf64_Off)offset == attestation->sh_offset) {
      offset += (off_t)attestation->sh_size;
      continue;
    }
    off_t limit = details.st_size;
    if ((Elf64_Off)offset < attestation->sh_offset)
      limit = (off_t)attestation->sh_offset;
    size_t wanted = sizeof(buffer);
    if ((off_t)wanted > limit - offset) wanted = (size_t)(limit - offset);
    ssize_t count = pread(descriptor, buffer, wanted, offset);
    if (count <= 0 || (size_t)count != wanted)
      failure("cannot compute frozen-runtime attested artifact digest");
    fs2_sha256_update(&artifact_context, buffer, wanted);
    offset += count;
  }
  unsigned char artifact_digest[32];
  fs2_sha256_final(&artifact_context, artifact_digest);
  if (memcmp(artifact_digest, raw + 44U, 32U) != 0)
    failure("frozen-runtime artifact differs from the independently signed digest");
  if (OPENSSL_init_crypto(OPENSSL_INIT_NO_LOAD_CONFIG, NULL) != 1)
    failure("frozen-runtime verifier cannot disable OpenSSL configuration");
  EVP_PKEY *key = EVP_PKEY_new_raw_public_key(EVP_PKEY_ED25519, NULL,
                                               public_key, sizeof(public_key));
  EVP_MD_CTX *context = EVP_MD_CTX_new();
  if (key == NULL || context == NULL ||
      EVP_DigestVerifyInit(context, NULL, NULL, NULL, key) != 1 ||
      EVP_DigestVerify(context, raw + 172U, 64U, raw, 172U) != 1) {
    EVP_MD_CTX_free(context);
    EVP_PKEY_free(key);
    failure("frozen-runtime Platform Security signature is invalid");
  }
  EVP_MD_CTX_free(context);
  EVP_PKEY_free(key);
}

static void fs2_require_frozen_runtime(int descriptor,
                                       void (*failure)(const char *)) {
  Elf64_Ehdr header;
  if (pread(descriptor, &header, sizeof(header), 0) != (ssize_t)sizeof(header) ||
      memcmp(header.e_ident, ELFMAG, SELFMAG) != 0 ||
      header.e_ident[EI_CLASS] != ELFCLASS64 ||
      header.e_ident[EI_DATA] != ELFDATA2LSB ||
      header.e_type != ET_DYN || header.e_machine != EM_X86_64 ||
      header.e_phentsize != sizeof(Elf64_Phdr) ||
      header.e_shentsize != sizeof(Elf64_Shdr) || header.e_shnum == 0U ||
      header.e_shstrndx == SHN_UNDEF || header.e_shstrndx >= header.e_shnum)
    failure("accepted frozen Python is not a supported ELF64 artifact");
  for (Elf64_Half index = 0; index < header.e_phnum; ++index) {
    Elf64_Phdr program;
    off_t offset = (off_t)header.e_phoff + (off_t)index * sizeof(program);
    if (pread(descriptor, &program, sizeof(program), offset) !=
        (ssize_t)sizeof(program))
      failure("accepted frozen Python program headers cannot be read");
    if (program.p_type == PT_INTERP)
      failure("accepted frozen Python must be an interpreter-free static PIE");
  }
  Elf64_Shdr names_section;
  off_t names_offset = (off_t)header.e_shoff +
                       (off_t)header.e_shstrndx * sizeof(names_section);
  if (pread(descriptor, &names_section, sizeof(names_section), names_offset) !=
          (ssize_t)sizeof(names_section) ||
      names_section.sh_size == 0U || names_section.sh_size > 1024U * 1024U)
    failure("frozen Python section-name table is invalid");
  char *names = calloc((size_t)names_section.sh_size + 1U, 1U);
  if (names == NULL ||
      pread(descriptor, names, (size_t)names_section.sh_size,
            (off_t)names_section.sh_offset) !=
          (ssize_t)names_section.sh_size)
    failure("cannot read frozen Python section-name table");
  static const char *const required_names[] = {
      ".fs2_frozen_module_inventory",
      ".fs2_frozen_module_payload",
      ".fs2_frozen_build_provenance",
      ".fs2_frozen_review_attestation",
      ".fs2_frozen_cpython_table",
  };
  Elf64_Shdr required_sections[5];
  Elf64_Half required_indices[5] = {0U, 0U, 0U, 0U, 0U};
  Elf64_Shdr symbol_table = {0};
  Elf64_Shdr symbol_strings = {0};
  Elf64_Shdr dynamic_section = {0};
  Elf64_Shdr relocation_section = {0};
  unsigned int dynamic_sections = 0U, relocation_sections = 0U;
  unsigned int found[5] = {0U, 0U, 0U, 0U, 0U};
  for (Elf64_Half index = 0; index < header.e_shnum; ++index) {
    Elf64_Shdr section;
    off_t offset = (off_t)header.e_shoff + (off_t)index * sizeof(section);
    if (pread(descriptor, &section, sizeof(section), offset) !=
        (ssize_t)sizeof(section) || section.sh_name >= names_section.sh_size)
      failure("frozen Python section header is invalid");
    const char *name = names + section.sh_name;
    if (memchr(name, '\0', (size_t)names_section.sh_size - section.sh_name) == NULL)
      failure("frozen Python section name is unterminated");
    for (size_t required = 0; required < 5U; ++required) {
      if (strcmp(name, required_names[required]) == 0) {
        if (++found[required] != 1U)
          failure("frozen Python contains a duplicate proof section");
        required_sections[required] = section;
        required_indices[required] = index;
      }
    }
    if (section.sh_type == SHT_SYMTAB) {
      if (symbol_table.sh_size != 0U || section.sh_link >= header.e_shnum)
        failure("frozen Python symbol table is absent or ambiguous");
      symbol_table = section;
      off_t linked_offset = (off_t)header.e_shoff +
                            (off_t)section.sh_link * sizeof(Elf64_Shdr);
      if (pread(descriptor, &symbol_strings, sizeof(symbol_strings),
                linked_offset) != (ssize_t)sizeof(symbol_strings))
        failure("cannot read frozen Python symbol string table");
    }
    if (section.sh_type == SHT_DYNAMIC) {
      dynamic_section = section;
      ++dynamic_sections;
    }
    if (section.sh_type == SHT_RELA && (section.sh_flags & SHF_ALLOC) != 0U) {
      relocation_section = section;
      ++relocation_sections;
    }
  }
  free(names);
  for (size_t index = 0; index < 5U; ++index)
    if (found[index] != 1U)
      failure("frozen Python omits a required proof section");
  if (dynamic_sections != 1U || relocation_sections != 1U)
    failure("static-PIE frozen Python lacks one exact dynamic relocation closure");
  struct fs2_pie_contract pie_contract = {0};
  fs2_verify_static_pie(descriptor, &header, &dynamic_section,
                        &relocation_section, &pie_contract, failure);
  for (size_t index = 0U; index < 5U; ++index) {
    if (required_sections[index].sh_type != SHT_PROGBITS ||
        required_sections[index].sh_size == 0U)
      failure("frozen-runtime proof/table section is not nonempty PROGBITS");
  }
  for (size_t index = 0U; index < 5U; ++index) {
    int must_load = index == 0U || index == 1U || index == 4U;
    if (must_load) {
      fs2_require_readonly_mapping(descriptor, &header,
                                   &required_sections[index],
                                   index == 4U, failure);
    } else if (required_sections[index].sh_flags != 0U ||
               required_sections[index].sh_addr != 0U) {
      failure("frozen provenance/attestation must be non-ALLOC metadata");
    }
  }
  const Elf64_Shdr *attestation = &required_sections[3];
  const Elf64_Shdr *provenance = &required_sections[2];
  Elf64_Off provenance_end = provenance->sh_offset + provenance->sh_size;
  if (provenance->sh_offset < header.e_ehsize ||
      !(provenance_end <= header.e_phoff ||
        provenance->sh_offset >=
            header.e_phoff + (Elf64_Off)header.e_phnum * header.e_phentsize) ||
      !(provenance_end <= header.e_shoff ||
        provenance->sh_offset >=
            header.e_shoff + (Elf64_Off)header.e_shnum * header.e_shentsize))
    failure("provenance metadata overlaps an ELF control table");
  for (Elf64_Half index = 0; index < header.e_shnum; ++index) {
    Elf64_Shdr section;
    off_t offset = (off_t)header.e_shoff + (off_t)index * sizeof(section);
    if (pread(descriptor, &section, sizeof(section), offset) !=
        (ssize_t)sizeof(section))
      failure("cannot re-read frozen-runtime section header");
    if (index == required_indices[2]) continue;
    if (section.sh_type != SHT_NOBITS && section.sh_size != 0U &&
        !(provenance_end <= section.sh_offset ||
          provenance->sh_offset >= section.sh_offset + section.sh_size))
      failure("provenance metadata overlaps another ELF section");
  }
  for (Elf64_Half index = 0; index < header.e_phnum; ++index) {
    Elf64_Phdr program;
    off_t offset = (off_t)header.e_phoff + (off_t)index * sizeof(program);
    if (pread(descriptor, &program, sizeof(program), offset) !=
        (ssize_t)sizeof(program))
      failure("cannot re-read provenance program headers");
    if (program.p_type == PT_LOAD && program.p_filesz != 0U &&
        !(provenance_end <= program.p_offset ||
          provenance->sh_offset >= program.p_offset + program.p_filesz))
      failure("provenance metadata overlaps a PT_LOAD segment");
  }
  Elf64_Off attestation_end = attestation->sh_offset + attestation->sh_size;
  if (attestation->sh_offset < header.e_ehsize ||
      !(attestation_end <= header.e_phoff ||
        attestation->sh_offset >=
            header.e_phoff + (Elf64_Off)header.e_phnum * header.e_phentsize) ||
      !(attestation_end <= header.e_shoff ||
        attestation->sh_offset >=
            header.e_shoff + (Elf64_Off)header.e_shnum * header.e_shentsize))
    failure("attestation normalization overlaps an ELF control table");
  for (Elf64_Half index = 0; index < header.e_shnum; ++index) {
    Elf64_Shdr section;
    off_t offset = (off_t)header.e_shoff + (off_t)index * sizeof(section);
    if (pread(descriptor, &section, sizeof(section), offset) !=
        (ssize_t)sizeof(section))
      failure("cannot re-read frozen-runtime section header");
    if (index == required_indices[3])
      continue;
    if (section.sh_type != SHT_NOBITS && section.sh_size != 0U &&
        !(attestation_end <= section.sh_offset ||
          attestation->sh_offset >= section.sh_offset + section.sh_size))
      failure("attestation normalization overlaps another ELF section");
  }
  for (Elf64_Half index = 0; index < header.e_phnum; ++index) {
    Elf64_Phdr program;
    off_t offset = (off_t)header.e_phoff + (off_t)index * sizeof(program);
    if (pread(descriptor, &program, sizeof(program), offset) !=
        (ssize_t)sizeof(program))
      failure("cannot re-read attestation program headers");
    if (program.p_type == PT_LOAD && program.p_filesz != 0U &&
        !(attestation_end <= program.p_offset ||
          attestation->sh_offset >= program.p_offset + program.p_filesz))
      failure("attestation normalization overlaps a PT_LOAD segment");
  }
  if (symbol_table.sh_size == 0U)
    failure("frozen Python omits the linkage symbol table");
  fs2_require_symbol(descriptor, &symbol_table, &symbol_strings,
                     "__fs2_frozen_inventory_start",
                     required_sections[0].sh_addr, 0U, failure);
  fs2_require_symbol(descriptor, &symbol_table, &symbol_strings,
                     "__fs2_frozen_inventory_end",
                     required_sections[0].sh_addr + required_sections[0].sh_size,
                     0U, failure);
  fs2_require_symbol(descriptor, &symbol_table, &symbol_strings,
                     "__fs2_frozen_payload_start",
                     required_sections[1].sh_addr, 0U, failure);
  fs2_require_symbol(descriptor, &symbol_table, &symbol_strings,
                     "__fs2_frozen_payload_end",
                     required_sections[1].sh_addr + required_sections[1].sh_size,
                     0U, failure);
  fs2_require_symbol(descriptor, &symbol_table, &symbol_strings,
                     "__fs2_frozen_table_start",
                     required_sections[4].sh_addr, 0U, failure);
  fs2_require_symbol(descriptor, &symbol_table, &symbol_strings,
                     "__fs2_frozen_table_end",
                     required_sections[4].sh_addr + required_sections[4].sh_size,
                     0U, failure);
  /* CPython exposes all four frozen authorities as pointer variables. Each
   * pointer storage location must independently relocate to the same reviewed
   * table; treating the symbols as table aliases models neither stock CPython
   * nor a supportable exact patch. */
  struct fs2_authority_storage authority_storage[5] = {0};
  fs2_require_pointer_symbol(descriptor, &header, &symbol_table,
                             &symbol_strings, "PyImport_FrozenModules",
                             required_sections[4].sh_addr, &pie_contract,
                             &authority_storage[0], failure);
  if (authority_storage[0].section.sh_type != SHT_PROGBITS)
    failure("PyImport_FrozenModules storage is not file-backed PROGBITS");
  fs2_require_readonly_mapping(descriptor, &header,
                               &authority_storage[0].section, 1,
                               failure);
  const char *const default_frozen_pointers[] = {
      "_PyImport_FrozenBootstrap", "_PyImport_FrozenStdlib",
      "_PyImport_FrozenTest", NULL};
  for (size_t index = 0U; default_frozen_pointers[index] != NULL; ++index) {
    fs2_require_pointer_symbol(
        descriptor, &header, &symbol_table, &symbol_strings,
        default_frozen_pointers[index], required_sections[4].sh_addr,
        &pie_contract, &authority_storage[index + 1U], failure);
    if (authority_storage[index + 1U].section.sh_type != SHT_PROGBITS)
      failure("default CPython frozen pointer storage is not file-backed");
    fs2_require_readonly_mapping(
        descriptor, &header, &authority_storage[index + 1U].section, 1,
        failure);
  }
  Elf64_Sym inittab = {0};
  Elf64_Shdr inittab_storage = {0};
  fs2_require_inittab_symbol(descriptor, &header, &symbol_table,
                             &symbol_strings, &inittab, &inittab_storage,
                             failure);
  authority_storage[4].section = inittab_storage;
  authority_storage[4].address = inittab.st_value;
  authority_storage[4].size = inittab.st_size;
  fs2_require_readonly_mapping(descriptor, &header, &inittab_storage, 1,
                               failure);
  fs2_verify_inittab(descriptor, &header, &inittab,
                     &required_sections[0], &pie_contract, failure);
  const Elf64_Shdr *core_authorities[] = {
      &required_sections[0], &required_sections[1], &required_sections[4],
  };
  for (size_t left = 0U; left < 3U; ++left)
    for (size_t right = left + 1U; right < 3U; ++right)
      fs2_require_disjoint_sections(core_authorities[left],
                                    core_authorities[right], failure);
  fs2_require_authority_storage_disjoint(
      core_authorities, 3U, authority_storage, 5U, failure);
  fs2_verify_inventory(descriptor, &required_sections[0],
                       &required_sections[1], failure);
  fs2_verify_cpython_table(descriptor, &required_sections[0],
                           &required_sections[1], &required_sections[4],
                           &pie_contract, failure);
  fs2_verify_attestation(descriptor, &required_sections[3],
                         &required_sections[0], &required_sections[1],
                         &required_sections[2], failure);
  free(pie_contract.relocations);
}

#endif
