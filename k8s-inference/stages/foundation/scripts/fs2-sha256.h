#ifndef FS2_SHA256_H
#define FS2_SHA256_H

/* Small dependency-free SHA-256 used only by the static pre-Python gates. */
#include <stddef.h>
#include <stdint.h>
#include <string.h>

struct fs2_sha256 {
  uint32_t state[8];
  uint64_t bytes;
  unsigned char block[64];
  size_t used;
};

static uint32_t fs2_rotr(uint32_t value, unsigned int shift) {
  return (value >> shift) | (value << (32U - shift));
}

static void fs2_sha256_transform(struct fs2_sha256 *context,
                                 const unsigned char block[64]) {
  static const uint32_t constants[64] = {
      0x428a2f98U, 0x71374491U, 0xb5c0fbcfU, 0xe9b5dba5U,
      0x3956c25bU, 0x59f111f1U, 0x923f82a4U, 0xab1c5ed5U,
      0xd807aa98U, 0x12835b01U, 0x243185beU, 0x550c7dc3U,
      0x72be5d74U, 0x80deb1feU, 0x9bdc06a7U, 0xc19bf174U,
      0xe49b69c1U, 0xefbe4786U, 0x0fc19dc6U, 0x240ca1ccU,
      0x2de92c6fU, 0x4a7484aaU, 0x5cb0a9dcU, 0x76f988daU,
      0x983e5152U, 0xa831c66dU, 0xb00327c8U, 0xbf597fc7U,
      0xc6e00bf3U, 0xd5a79147U, 0x06ca6351U, 0x14292967U,
      0x27b70a85U, 0x2e1b2138U, 0x4d2c6dfcU, 0x53380d13U,
      0x650a7354U, 0x766a0abbU, 0x81c2c92eU, 0x92722c85U,
      0xa2bfe8a1U, 0xa81a664bU, 0xc24b8b70U, 0xc76c51a3U,
      0xd192e819U, 0xd6990624U, 0xf40e3585U, 0x106aa070U,
      0x19a4c116U, 0x1e376c08U, 0x2748774cU, 0x34b0bcb5U,
      0x391c0cb3U, 0x4ed8aa4aU, 0x5b9cca4fU, 0x682e6ff3U,
      0x748f82eeU, 0x78a5636fU, 0x84c87814U, 0x8cc70208U,
      0x90befffaU, 0xa4506cebU, 0xbef9a3f7U, 0xc67178f2U,
  };
  uint32_t words[64];
  for (size_t index = 0; index < 16; ++index) {
    words[index] = ((uint32_t)block[index * 4] << 24) |
                   ((uint32_t)block[index * 4 + 1] << 16) |
                   ((uint32_t)block[index * 4 + 2] << 8) |
                   (uint32_t)block[index * 4 + 3];
  }
  for (size_t index = 16; index < 64; ++index) {
    uint32_t first = fs2_rotr(words[index - 15], 7) ^
                     fs2_rotr(words[index - 15], 18) ^
                     (words[index - 15] >> 3);
    uint32_t second = fs2_rotr(words[index - 2], 17) ^
                      fs2_rotr(words[index - 2], 19) ^
                      (words[index - 2] >> 10);
    words[index] = words[index - 16] + first + words[index - 7] + second;
  }
  uint32_t a = context->state[0], b = context->state[1];
  uint32_t c = context->state[2], d = context->state[3];
  uint32_t e = context->state[4], f = context->state[5];
  uint32_t g = context->state[6], h = context->state[7];
  for (size_t index = 0; index < 64; ++index) {
    uint32_t upper = fs2_rotr(e, 6) ^ fs2_rotr(e, 11) ^ fs2_rotr(e, 25);
    uint32_t choose = (e & f) ^ ((~e) & g);
    uint32_t first = h + upper + choose + constants[index] + words[index];
    uint32_t lower = fs2_rotr(a, 2) ^ fs2_rotr(a, 13) ^ fs2_rotr(a, 22);
    uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
    uint32_t second = lower + majority;
    h = g; g = f; f = e; e = d + first;
    d = c; c = b; b = a; a = first + second;
  }
  context->state[0] += a; context->state[1] += b;
  context->state[2] += c; context->state[3] += d;
  context->state[4] += e; context->state[5] += f;
  context->state[6] += g; context->state[7] += h;
}

static void fs2_sha256_init(struct fs2_sha256 *context) {
  static const uint32_t initial[8] = {
      0x6a09e667U, 0xbb67ae85U, 0x3c6ef372U, 0xa54ff53aU,
      0x510e527fU, 0x9b05688cU, 0x1f83d9abU, 0x5be0cd19U,
  };
  memcpy(context->state, initial, sizeof(initial));
  context->bytes = 0;
  context->used = 0;
}

static void fs2_sha256_update(struct fs2_sha256 *context,
                              const unsigned char *input, size_t length) {
  context->bytes += length;
  while (length > 0) {
    size_t available = 64U - context->used;
    size_t take = length < available ? length : available;
    memcpy(context->block + context->used, input, take);
    context->used += take;
    input += take;
    length -= take;
    if (context->used == 64U) {
      fs2_sha256_transform(context, context->block);
      context->used = 0;
    }
  }
}

static void fs2_sha256_final(struct fs2_sha256 *context,
                             unsigned char output[32]) {
  uint64_t bits = context->bytes * 8U;
  unsigned char padding[128] = {0x80};
  size_t padding_length = context->used < 56U ? 56U - context->used
                                               : 120U - context->used;
  fs2_sha256_update(context, padding, padding_length);
  unsigned char length_bytes[8];
  for (size_t index = 0; index < 8; ++index)
    length_bytes[7U - index] = (unsigned char)(bits >> (index * 8U));
  fs2_sha256_update(context, length_bytes, sizeof(length_bytes));
  for (size_t index = 0; index < 8; ++index) {
    output[index * 4] = (unsigned char)(context->state[index] >> 24);
    output[index * 4 + 1] = (unsigned char)(context->state[index] >> 16);
    output[index * 4 + 2] = (unsigned char)(context->state[index] >> 8);
    output[index * 4 + 3] = (unsigned char)context->state[index];
  }
}

#endif
