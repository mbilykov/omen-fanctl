#include <cuda_runtime.h>

#include <chrono>
#include <cstdio>
#include <cstdlib>

static void check(cudaError_t result, const char *operation) {
    if (result != cudaSuccess) {
        std::fprintf(stderr, "%s failed: %s\n", operation,
                     cudaGetErrorString(result));
        std::exit(1);
    }
}

__global__ void fma_stress(float *left, float *right, std::size_t count) {
    const std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    if (index >= count) {
        return;
    }

    float a = left[index] + static_cast<float>(index & 255) * 0.000001f;
    float b = right[index] + 0.00001f;
    #pragma unroll 8
    for (int iteration = 0; iteration < 2048; ++iteration) {
        a = fmaf(a, 1.000000119f, b);
        b = fmaf(b, 0.999999881f, a * 0.0000001f);
    }
    left[index] = a;
    right[index] = b;
}

int main(int argc, char **argv) {
    const int duration_seconds = argc > 1 ? std::atoi(argv[1]) : 120;
    if (duration_seconds < 1 || duration_seconds > 600) {
        std::fprintf(stderr, "duration must be between 1 and 600 seconds\n");
        return 2;
    }

    int device_count = 0;
    check(cudaGetDeviceCount(&device_count), "cudaGetDeviceCount");
    if (device_count < 1) {
        std::fprintf(stderr, "no CUDA device found\n");
        return 1;
    }

    cudaDeviceProp properties{};
    check(cudaGetDeviceProperties(&properties, 0), "cudaGetDeviceProperties");
    check(cudaSetDevice(0), "cudaSetDevice");

    const int threads = 256;
    const int blocks = properties.multiProcessorCount * 64;
    const std::size_t count = static_cast<std::size_t>(threads) * blocks;
    const std::size_t bytes = count * sizeof(float);
    float *left = nullptr;
    float *right = nullptr;
    check(cudaMalloc(&left, bytes), "cudaMalloc(left)");
    check(cudaMalloc(&right, bytes), "cudaMalloc(right)");
    check(cudaMemset(left, 0, bytes), "cudaMemset(left)");
    check(cudaMemset(right, 1, bytes), "cudaMemset(right)");

    std::printf("GPU: %s, SMs: %d, duration: %d s\n", properties.name,
                properties.multiProcessorCount, duration_seconds);
    std::fflush(stdout);

    const auto started = std::chrono::steady_clock::now();
    unsigned long long launches = 0;
    while (std::chrono::duration<double>(std::chrono::steady_clock::now() - started)
               .count() < duration_seconds) {
        fma_stress<<<blocks, threads>>>(left, right, count);
        check(cudaGetLastError(), "kernel launch");
        check(cudaDeviceSynchronize(), "kernel execution");
        ++launches;
    }

    check(cudaFree(left), "cudaFree(left)");
    check(cudaFree(right), "cudaFree(right)");
    std::printf("completed %llu kernel launches\n", launches);
    return 0;
}
