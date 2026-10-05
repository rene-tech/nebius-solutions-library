/* Bounded host-memory correctness control for GROMACS-sized startup traffic.
 * Not a bandwidth benchmark. The launcher, ranks, GPU/NIC mapping and UCX
 * settings are the same as the task's immutable MPI worker. */
#include <mpi.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int rank_id;

static void check(int code) {
    if (code == MPI_SUCCESS) return;
    char message[MPI_MAX_ERROR_STRING];
    int length;
    MPI_Error_string(code, message, &length);
    fprintf(stderr, "rank %d: %.*s\n", rank_id, length, message);
    MPI_Abort(MPI_COMM_WORLD, 2);
    exit(2);
}

static void *allocate(size_t bytes) {
    void *value = malloc(bytes);
    if (!value) {
        fprintf(stderr, "rank %d: cannot allocate %zu bytes\n", rank_id, bytes);
        MPI_Abort(MPI_COMM_WORLD, 2);
        exit(2);
    }
    return value;
}

static void verify(const int *buffer, int count, int source, int destination) {
    for (int i = 0; i < count; ++i) {
        if (buffer[i] != source * 1000000 + destination * 10000 + i % 997) {
            fprintf(stderr, "rank %d: content mismatch at %d from %d to %d\n",
                    rank_id, i, source, destination);
            MPI_Abort(MPI_COMM_WORLD, 2);
            exit(2);
        }
    }
}

static void fill(int *buffer, int count, int source, int destination) {
    for (int i = 0; i < count; ++i)
        buffer[i] = source * 1000000 + destination * 10000 + i % 997;
}

static double begin(const char *name, size_t bytes) {
    printf("{\"kind\":\"host-collective-begin\",\"rank\":%d,\"collective\":\"%s\",\"message_bytes\":%zu}\n",
           rank_id, name, bytes);
    fflush(stdout);
    check(MPI_Barrier(MPI_COMM_WORLD));
    return MPI_Wtime();
}

static void finish(const char *name, size_t bytes, double started) {
    check(MPI_Barrier(MPI_COMM_WORLD));
    if (rank_id == 0) {
        printf("{\"kind\":\"host-collective\",\"collective\":\"%s\",\"message_bytes\":%zu,\"status\":\"passed\",\"wall_seconds\":%.9f}\n",
               name, bytes, MPI_Wtime() - started);
        fflush(stdout);
    }
}

int main(int argc, char **argv) {
    int size;
    /* First size exceeds the exact 8,996,516-byte finite TPR, then 16/32 MiB. */
    const int counts[] = {2250000, 4194304, 8388608};
    check(MPI_Init(&argc, &argv));
    check(MPI_Comm_rank(MPI_COMM_WORLD, &rank_id));
    check(MPI_Comm_size(MPI_COMM_WORLD, &size));
    check(MPI_Comm_set_errhandler(MPI_COMM_WORLD, MPI_ERRORS_RETURN));
    if (size != 16) MPI_Abort(MPI_COMM_WORLD, 2);
    for (int n = 0; n < 3; ++n) {
        int count = counts[n];
        size_t bytes = (size_t)count * sizeof(int);
        int *buffer = allocate(bytes);
        if (rank_id == 0) fill(buffer, count, 0, 0);
        else memset(buffer, 0xff, bytes);
        double started = begin("Bcast", bytes);
        check(MPI_Bcast(buffer, count, MPI_INT, 0, MPI_COMM_WORLD));
        verify(buffer, count, 0, 0);
        finish("Bcast", bytes, started);

        int *send = allocate(bytes * (size_t)size);
        int *receive = allocate(bytes * (size_t)size);
        int *sizes = allocate((size_t)size * sizeof(int));
        int *offsets = allocate((size_t)size * sizeof(int));
        for (int peer = 0; peer < size; ++peer) {
            sizes[peer] = count;
            offsets[peer] = peer * count;
            fill(send + (size_t)peer * count, count, rank_id, peer);
        }
        memset(buffer, 0xff, bytes);
        started = begin("Scatterv", bytes);
        check(MPI_Scatterv(send, sizes, offsets, MPI_INT, buffer, count,
                           MPI_INT, 0, MPI_COMM_WORLD));
        verify(buffer, count, 0, rank_id);
        finish("Scatterv", bytes, started);

        memset(receive, 0xff, bytes * (size_t)size);
        started = begin("Alltoall", bytes);
        check(MPI_Alltoall(send, count, MPI_INT, receive, count, MPI_INT,
                          MPI_COMM_WORLD));
        for (int peer = 0; peer < size; ++peer)
            verify(receive + (size_t)peer * count, count, peer, rank_id);
        finish("Alltoall", bytes, started);
        free(offsets);
        free(sizes);
        free(receive);
        free(send);
        free(buffer);
    }
    if (rank_id == 0) {
        printf("{\"kind\":\"host-collectives-summary\",\"status\":\"passed\",\"ranks\":16,\"message_bytes\":[9000000,16777216,33554432],\"checks\":[\"Bcast\",\"Scatterv\",\"Alltoall\"],\"performance_claim\":false}\n");
        fflush(stdout);
    }
    check(MPI_Finalize());
    return 0;
}
