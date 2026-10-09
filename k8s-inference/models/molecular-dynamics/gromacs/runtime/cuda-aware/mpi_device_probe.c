/* Bounded correctness test, not a bandwidth or GPUDirect/RDMA benchmark.
 * MPI_Init precedes MPIX_Query_cuda_support. MPI receives actual cudaMalloc
 * pointers; host copies occur only in test preparation and verification. */
#include <cuda_runtime_api.h>
#include <mpi.h>
#include <mpi-ext.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if !defined(MPIX_CUDA_AWARE_SUPPORT) || MPIX_CUDA_AWARE_SUPPORT != 1
#error "Open MPI must be compiled with CUDA buffer support"
#endif

static int rank_id;

static void fail(const char *message) {
    fprintf(stderr, "rank %d: %s\n", rank_id, message);
    MPI_Abort(MPI_COMM_WORLD, 2);
    exit(2);
}

static void cuda_check(cudaError_t code) {
    if (code != cudaSuccess) fail(cudaGetErrorString(code));
}

static void mpi_check(int code) {
    if (code != MPI_SUCCESS) fail("MPI call failed");
}

static int value(int rank, int index, int iteration) {
    return rank * 1000000 + index % 97 + iteration;
}

int main(int argc, char **argv) {
    int size, devices, local_rank = 0, query, driver, runtime;
    int counts[] = {1, 1024, 262144};
    int *send_device, *recv_device, *host;
    struct cudaDeviceProp properties;
    if (MPI_Init(&argc, &argv) != MPI_SUCCESS) return 2;
    mpi_check(MPI_Comm_rank(MPI_COMM_WORLD, &rank_id));
    mpi_check(MPI_Comm_size(MPI_COMM_WORLD, &size));
    mpi_check(MPI_Comm_set_errhandler(MPI_COMM_WORLD, MPI_ERRORS_RETURN));
    if (size < 1 || size > 16) fail("probe rank count must be 1..16");
    if (getenv("GMX_FORCE_GPU_AWARE_MPI")) fail("force override is forbidden");
    if (getenv("OMPI_COMM_WORLD_LOCAL_RANK")) local_rank = atoi(getenv("OMPI_COMM_WORLD_LOCAL_RANK"));
    cuda_check(cudaGetDeviceCount(&devices));
    int device = devices == 1 ? 0 : local_rank;
    if (devices < 1 || device < 0 || device >= devices) fail("rank has no unique visible device");
    cuda_check(cudaSetDevice(device));
    cuda_check(cudaGetDeviceProperties(&properties, device));
    cuda_check(cudaDriverGetVersion(&driver));
    cuda_check(cudaRuntimeGetVersion(&runtime));
    query = MPIX_Query_cuda_support();
    if (query != 1) fail("initialized MPIX_Query_cuda_support is not 1");
    unsigned char *uuids = malloc((size_t)size * 16);
    if (!uuids) fail("host allocation failed");
    mpi_check(MPI_Allgather(properties.uuid.bytes, 16, MPI_BYTE, uuids, 16, MPI_BYTE, MPI_COMM_WORLD));
    for (int i = 0; i < size; ++i)
        for (int j = i + 1; j < size; ++j)
            if (!memcmp(uuids + i * 16, uuids + j * 16, 16)) fail("two ranks mapped to the same physical GPU");
    char uuid_hex[33];
    for (int i = 0; i < 16; ++i) snprintf(uuid_hex + 2 * i, 3, "%02x", (unsigned char)properties.uuid.bytes[i]);
    printf("{\"kind\":\"rank\",\"rank\":%d,\"local_rank\":%d,\"device\":%d,\"gpu_uuid_hex\":\"%s\",\"gpu_name\":\"%s\",\"driver_api\":%d,\"runtime_api\":%d,\"MPIX_Query_cuda_support\":%d}\n",
           rank_id, local_rank, device, uuid_hex, properties.name, driver, runtime, query);
    fflush(stdout);
    host = malloc((size_t)counts[2] * sizeof(int));
    if (!host) fail("host allocation failed");
    cuda_check(cudaMalloc((void **)&send_device, (size_t)counts[2] * sizeof(int)));
    cuda_check(cudaMalloc((void **)&recv_device, (size_t)counts[2] * sizeof(int)));
    int from = (rank_id + size - 1) % size, to = (rank_id + 1) % size;
    double started = MPI_Wtime();
    for (int c = 0; c < 3; ++c) {
        int count = counts[c];
        size_t bytes = (size_t)count * sizeof(int);
        for (int iteration = 0; iteration < 3; ++iteration) {
            for (int i = 0; i < count; ++i) host[i] = value(rank_id, i, iteration);
            cuda_check(cudaMemcpy(send_device, host, bytes, cudaMemcpyHostToDevice));
            cuda_check(cudaMemset(recv_device, 0xff, bytes));
            cuda_check(cudaDeviceSynchronize());
            mpi_check(MPI_Sendrecv(send_device, count, MPI_INT, to, 101,
                                   recv_device, count, MPI_INT, from, 101,
                                   MPI_COMM_WORLD, MPI_STATUS_IGNORE));
            cuda_check(cudaMemcpy(host, recv_device, bytes, cudaMemcpyDeviceToHost));
            for (int i = 0; i < count; ++i)
                if (host[i] != value(from, i, iteration)) fail("device Sendrecv contents differ");
            cuda_check(cudaMemset(recv_device, 0xff, bytes));
            cuda_check(cudaDeviceSynchronize());
            MPI_Request requests[2];
            mpi_check(MPI_Irecv(recv_device, count, MPI_INT, from, 102, MPI_COMM_WORLD, &requests[0]));
            mpi_check(MPI_Isend(send_device, count, MPI_INT, to, 102, MPI_COMM_WORLD, &requests[1]));
            mpi_check(MPI_Waitall(2, requests, MPI_STATUSES_IGNORE));
            cuda_check(cudaMemcpy(host, recv_device, bytes, cudaMemcpyDeviceToHost));
            for (int i = 0; i < count; ++i)
                if (host[i] != value(from, i, iteration)) fail("device Isend/Irecv contents differ");
        }
        for (int i = 0; i < count; ++i) host[i] = rank_id + 1;
        cuda_check(cudaMemcpy(send_device, host, bytes, cudaMemcpyHostToDevice));
        mpi_check(MPI_Allreduce(send_device, recv_device, count, MPI_INT, MPI_SUM, MPI_COMM_WORLD));
        cuda_check(cudaMemcpy(host, recv_device, bytes, cudaMemcpyDeviceToHost));
        for (int i = 0; i < count; ++i)
            if (host[i] != size * (size + 1) / 2) fail("device Allreduce contents differ");
    }
    mpi_check(MPI_Barrier(MPI_COMM_WORLD));
    if (rank_id == 0) {
        printf("{\"kind\":\"summary\",\"status\":\"passed\",\"ranks\":%d,\"message_bytes\":[4,4096,1048576],\"point_to_point_repeats\":3,\"checks\":[\"Sendrecv\",\"Isend/Irecv/Waitall\",\"Allreduce\"],\"wall_seconds\":%.9f,\"performance_claim\":false}\n", size, MPI_Wtime() - started);
        fflush(stdout);
    }
    cuda_check(cudaFree(send_device));
    cuda_check(cudaFree(recv_device));
    free(host);
    free(uuids);
    mpi_check(MPI_Finalize());
    return 0;
}
