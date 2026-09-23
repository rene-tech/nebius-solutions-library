#!/bin/bash
set -euo pipefail
case "${FFT_KOKKOS}" in KISS|CUFFT) ;; *) exit 2;; esac
case "${LAMMPS_ARCH}:${INSTALL_ARCH}" in 90:90) kokkos_arch=HOPPER90;; 86:86) kokkos_arch=AMPERE86;; 89:86) kokkos_arch=ADA89;; *) exit 2;; esac
case "${BUILD_JOBS}" in [1-8]) ;; *) exit 2;; esac
export CC=gcc-13 CXX=g++-13 NVCC_WRAPPER_DEFAULT_COMPILER=g++-13
export CFLAGS='-march=x86-64-v3 -mtune=generic -O3 -pipe'
export CXXFLAGS="${CFLAGS}" LDFLAGS=-Wl,--as-needed
sed -i -E "s/^default_arch=(.*)/default_arch=sm_${LAMMPS_ARCH}/g" /source/lib/kokkos/bin/nvcc_wrapper
cmake -S /source/cmake -B /build \
  -DCMAKE_INSTALL_PREFIX="/usr/local/lammps/sm${INSTALL_ARCH}" \
  -DCMAKE_EXE_LINKER_FLAGS=-L/usr/local/cuda/lib64 \
  -DBUILD_SHARED_LIBS=ON -DCMAKE_BUILD_TYPE=Release \
  -DKokkos_ARCH_${kokkos_arch}=ON -DKokkos_ARCH_HSW=ON \
  -DMPI_C_COMPILER=mpicc -DMPI_CXX_COMPILER=mpicxx \
  -DBUILD_MPI=yes -DBUILD_OMP=yes \
  -DCMAKE_CXX_COMPILER=/source/lib/kokkos/bin/nvcc_wrapper \
  -DPKG_PYTHON=off -DPKG_KSPACE=yes -DPKG_MOLECULE=yes \
  -DPKG_REPLICA=yes -DPKG_RIGID=yes -DPKG_MISC=yes \
  -DPKG_MANYBODY=yes -DPKG_ASPHERE=yes -DPKG_KOKKOS=yes \
  -DFFT=FFTW3 -DFFT_KOKKOS="${FFT_KOKKOS}" \
  -DFFTW3_INCLUDE_DIR=/usr/local/fftw/include \
  -DFFTW3_LIBRARY=/usr/local/fftw/lib/libfftw3.so \
  -DFFTW3_OMP_LIBRARY=/usr/local/fftw/lib/libfftw3_omp.so \
  -DKokkos_ENABLE_CUDA=yes -DKokkos_ENABLE_HWLOC=yes \
  -DCMAKE_TUNE_FLAGS="${CFLAGS}" \
  -DCMAKE_C_FLAGS_RELEASE="${CFLAGS} -DNDEBUG" \
  -DCMAKE_CXX_FLAGS_RELEASE="${CXXFLAGS} -DNDEBUG" \
  -DPKG_OPENMP=yes -DPKG_REAXFF=yes -DPKG_ML-SNAP=yes \
  -DPKG_DPD-BASIC=yes -DPKG_MC=yes -DPKG_GPU=no \
  -DPKG_CLASS2=yes -DPKG_DIPOLE=yes -DPKG_DPD-SMOOTH=yes \
  -DPKG_EXTRA-MOLECULE=yes -DPKG_OPT=yes \
  -DKokkos_ENABLE_IMPL_CUDA_MALLOC_ASYNC=OFF
cmake --build /build --parallel "${BUILD_JOBS}"
cmake --install /build
mkdir /build-provenance
cp /build/CMakeCache.txt /build-provenance/
git -C /source rev-parse HEAD > /build-provenance/source-revision.txt
git -C /source diff > /build-provenance/source-diff.patch
dpkg-query -W > /build-provenance/packages.tsv
gcc-13 --version > /build-provenance/gcc.txt
nvcc --version > /build-provenance/nvcc.txt
cmake --version > /build-provenance/cmake.txt
sha256sum /usr/local/lammps/sm${INSTALL_ARCH}/lib/liblammps.so.0 /usr/local/lammps/sm${INSTALL_ARCH}/bin/lmp > /build-provenance/native-sha256.txt
LD_LIBRARY_PATH=/usr/local/lammps/sm${INSTALL_ARCH}/lib:${LD_LIBRARY_PATH:-} /usr/local/lammps/sm${INSTALL_ARCH}/bin/lmp -h > /build-provenance/lammps-help.txt
