# Packaged live-acceptance assets

The all-model live harness sends licensed image fixtures as self-contained data
URIs. This avoids making model qualification depend on a third-party host or on
the reputation of a cluster egress address. Each filename is the exact SHA-256
of its bytes; the harness also checks the catalog-recorded length, JPEG magic,
and digest before constructing a request.

`4cbbcf805291db949e4ff085ca3c7258b2823de21b2857ae684e6c91ff9a38a4.jpg`
is the CC0-1.0 normal posteroanterior chest radiograph recorded in the catalog:

- source: <https://upload.wikimedia.org/wikipedia/commons/a/a1/Normal_posteroanterior_%28PA%29_chest_radiograph_%28X-ray%29.jpg>
- bytes: `906335`
- SHA-256: `4cbbcf805291db949e4ff085ca3c7258b2823de21b2857ae684e6c91ff9a38a4`
