#!/usr/bin/env bash
set -euo pipefail

test "${BUILD_JOBS}" -ge 1
test "${BUILD_JOBS}" -le 16
printf '%s  %s\n' "${PMEMD_SOURCE_SHA256}" /pmemd-source/pmemd26.tar.bz2 | sha256sum --check -
mkdir -p /source /build-provenance
tar -xjf /pmemd-source/pmemd26.tar.bz2 -C /source
patch --directory=/source/pmemd26_src --strip=1 --forward < /opt/fs2-build/cuda-sm89-sm90.patch
cmake -S /source/pmemd26_src -B /build -Wno-dev \
    -DCMAKE_INSTALL_PREFIX=/opt/amber26 -DCMAKE_BUILD_TYPE=Release \
    -DCOMPILER=GNU -DCMAKE_C_COMPILER=gcc-13 -DCMAKE_CXX_COMPILER=g++-13 \
    -DCMAKE_Fortran_COMPILER=gfortran-13 \
    -DCUDA=TRUE -DMPI=FALSE -DNCCL=FALSE -DGTI=TRUE -DPGM=FALSE \
    -DPMEMD_ONLY=TRUE -DINSTALL_TESTS=TRUE -DCHECK_UPDATES=FALSE \
    -DDOWNLOAD_MINICONDA=FALSE -DBUILD_PYTHON=FALSE -DBUILD_PERL=FALSE -DBUILD_GUI=FALSE
cmake --build /build --parallel "${BUILD_JOBS}"
cmake --install /build
test -x /opt/amber26/bin/pmemd
test -x /opt/amber26/bin/pmemd.cuda_SPFP
test -x /opt/amber26/bin/pmemd.cuda_DPFP
cp /source/pmemd26_src/README /build-provenance/LICENSE-NOTICE
cp /opt/fs2-build/cuda-sm89-sm90.patch /build-provenance/
cp /build/CMakeCache.txt /build-provenance/
printf '%s\n' "${PMEMD_SOURCE_SHA256}" > /build-provenance/source-archive.sha256
dpkg-query --show --showformat='${Package}\t${Version}\n' > /build-provenance/build-packages.tsv
nvcc --version > /build-provenance/nvcc-version.txt
gfortran-13 --version > /build-provenance/fortran-version.txt
sha256sum /opt/amber26/bin/pmemd /opt/amber26/bin/pmemd.cuda_SPFP /opt/amber26/bin/pmemd.cuda_DPFP > /build-provenance/binaries.sha256
cuobjdump -lelf /opt/amber26/bin/pmemd.cuda_SPFP > /build-provenance/spfp-cubins.txt
cuobjdump -lelf /opt/amber26/bin/pmemd.cuda_DPFP > /build-provenance/dpfp-cubins.txt
grep -q 'sm_89' /build-provenance/spfp-cubins.txt
grep -q 'sm_90' /build-provenance/spfp-cubins.txt
# The privately mounted archive and source tree never enter the runtime image.
