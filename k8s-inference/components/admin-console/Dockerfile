# syntax=docker/dockerfile:1.12@sha256:93bfd3b68c109427185cd78b4779fc82b484b0b7618e36d0f104d4d801e66d25

FROM node:22.22.2-alpine3.23@sha256:8ea2348b068a9544dae7317b4f3aafcdc032df1647bb7d768a05a5cad1a7683f AS builder

WORKDIR /workspace/k8s-inference/components/admin-console
COPY k8s-inference/components/admin-console/package.json k8s-inference/components/admin-console/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund

COPY k8s-inference/components/admin-console/index.html ./
COPY k8s-inference/components/admin-console/tsconfig.json k8s-inference/components/admin-console/tsconfig.app.json k8s-inference/components/admin-console/tsconfig.node.json ./
COPY k8s-inference/components/admin-console/vite.config.ts ./
COPY k8s-inference/components/admin-console/src ./src
RUN npm run build \
    && test -f dist/index.html \
    && test -n "$(find dist/assets -maxdepth 1 -type f -name 'index-*.js' -print -quit)" \
    && ! grep -R -E 'project-u02|mk8scluster-|mk8snodegroup-|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY|Bearer [A-Za-z0-9._-]{16,}' dist

FROM nginxinc/nginx-unprivileged:1.31.3-alpine3.24@sha256:f972e5322b9797dc2a6b830030094426437b1ae7032e4644496395336ac6fdac

COPY --chown=101:101 k8s-inference/components/admin-console/nginx-main.conf /etc/nginx/nginx.conf
COPY --chown=101:101 k8s-inference/components/admin-console/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder --chown=101:101 /workspace/k8s-inference/components/admin-console/dist /usr/share/nginx/html/admin

ARG FS2_SOURCE_COMMIT
ARG FS2_SOURCE_TREE
ARG FS2_PACKAGE_LOCK_SHA256
RUN test -n "$FS2_SOURCE_COMMIT" \
    && test -n "$FS2_SOURCE_TREE" \
    && test -n "$FS2_PACKAGE_LOCK_SHA256"
LABEL org.opencontainers.image.source="https://github.com/rene-tech/nebius-solutions-library" \
      org.opencontainers.image.revision="$FS2_SOURCE_COMMIT" \
      org.opencontainers.image.base.name="docker.io/nginxinc/nginx-unprivileged:1.31.3-alpine3.24" \
      org.opencontainers.image.base.digest="sha256:f972e5322b9797dc2a6b830030094426437b1ae7032e4644496395336ac6fdac" \
      ai.nebius.fs2-serve.source-tree="$FS2_SOURCE_TREE" \
      ai.nebius.fs2-serve.package-lock-sha256="$FS2_PACKAGE_LOCK_SHA256" \
      ai.nebius.fs2-serve.node-builder.digest="sha256:8ea2348b068a9544dae7317b4f3aafcdc032df1647bb7d768a05a5cad1a7683f"

USER 101:101
EXPOSE 8080
STOPSIGNAL SIGQUIT
CMD ["nginx", "-g", "daemon off;"]
