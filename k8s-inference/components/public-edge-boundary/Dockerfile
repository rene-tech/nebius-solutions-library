ARG FS2_GO_BUILDER_IMAGE
FROM ${FS2_GO_BUILDER_IMAGE} AS build
WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -buildvcs=false -ldflags='-s -w' -o /out/public-edge-boundary ./cmd/webhook \
 && CGO_ENABLED=0 GOOS=linux go build -trimpath -buildvcs=false -ldflags='-s -w' -o /out/public-edge-collector ./cmd/collector \
 && CGO_ENABLED=0 GOOS=linux go build -trimpath -buildvcs=false -ldflags='-s -w' -o /out/public-edge-native-authority ./cmd/native-authority \
 && CGO_ENABLED=0 GOOS=linux go build -trimpath -buildvcs=false -ldflags='-s -w' -o /out/public-edge-snapshot-authority ./cmd/snapshot-authority \
 && CGO_ENABLED=0 GOOS=linux go build -trimpath -buildvcs=false -ldflags='-s -w' -o /out/public-edge-custody-installer ./cmd/custody-installer \
 && CGO_ENABLED=0 GOOS=linux go build -trimpath -buildvcs=false -ldflags='-s -w' -o /out/public-edge-mtls-probe ./cmd/mtls-probe \
 && CGO_ENABLED=0 GOOS=linux go build -trimpath -buildvcs=false -ldflags='-s -w' -o /out/public-edge-enrollment-compiler ./cmd/enrollment-compiler \
 && CGO_ENABLED=0 GOOS=linux go build -trimpath -buildvcs=false -ldflags='-s -w' -o /out/public-edge-enrollment-assembler ./cmd/enrollment-assembler \
 && CGO_ENABLED=0 GOOS=linux go build -trimpath -buildvcs=false -ldflags='-s -w' -o /out/public-edge-trust-bundle-gate ./cmd/trust-bundle-gate

FROM scratch AS enrollment-tools-output
COPY --from=build /out/public-edge-enrollment-compiler /public-edge-enrollment-compiler
COPY --from=build /out/public-edge-enrollment-assembler /public-edge-enrollment-assembler
COPY --from=build /out/public-edge-snapshot-authority /public-edge-snapshot-authority

FROM scratch AS developer-runtime
LABEL ai.nebius.fs2.public-edge.production="false" \
      ai.nebius.fs2.public-edge.promotion="forbidden-empty-trust"
COPY --from=build /out/public-edge-boundary /usr/local/bin/public-edge-boundary
COPY --from=build /out/public-edge-collector /usr/local/bin/public-edge-collector
COPY --from=build /out/public-edge-native-authority /usr/local/bin/public-edge-native-authority
COPY --from=build /out/public-edge-snapshot-authority /usr/local/bin/public-edge-snapshot-authority
COPY --from=build /out/public-edge-custody-installer /usr/local/bin/public-edge-custody-installer
COPY --from=build /out/public-edge-mtls-probe /usr/local/bin/public-edge-mtls-probe
COPY config/trusted-acceptance-issuers.json /usr/local/share/fs2-boundary/trusted-acceptance-issuers.json
COPY config/native-collector.json /usr/local/share/fs2-boundary/native-collector.json
COPY config/native-authority.json /usr/local/share/fs2-boundary/native-authority.json
COPY config/transition-settlement.json /usr/local/share/fs2-boundary/transition-settlement.json
COPY config/trusted-native-response-issuers.json /usr/local/share/fs2-boundary/trusted-native-response-issuers.json
COPY config/trusted-snapshot-issuers.json /usr/local/share/fs2-boundary/trusted-snapshot-issuers.json
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/public-edge-boundary"]

FROM build AS production-trust-gate
ARG FS2_PRODUCTION_ACCEPTANCE_TRUST_FILE=config/trusted-acceptance-issuers.json
ARG FS2_PRODUCTION_ACCEPTANCE_TRUST_SHA256
ARG FS2_PRODUCTION_NATIVE_TRUST_FILE=config/trusted-native-response-issuers.json
ARG FS2_PRODUCTION_NATIVE_TRUST_SHA256
ARG FS2_PRODUCTION_SNAPSHOT_TRUST_FILE=config/trusted-snapshot-issuers.json
ARG FS2_PRODUCTION_SNAPSHOT_TRUST_SHA256
COPY ${FS2_PRODUCTION_ACCEPTANCE_TRUST_FILE} /production-trust/trusted-acceptance-issuers.json
COPY ${FS2_PRODUCTION_NATIVE_TRUST_FILE} /production-trust/trusted-native-response-issuers.json
COPY ${FS2_PRODUCTION_SNAPSHOT_TRUST_FILE} /production-trust/trusted-snapshot-issuers.json
RUN /out/public-edge-trust-bundle-gate /production-trust/trusted-acceptance-issuers.json "${FS2_PRODUCTION_ACCEPTANCE_TRUST_SHA256}" fs2-serve.nebius.ai/public-edge-boundary-acceptance-trust/v1 platform-security-public-edge-boundary-acceptance platform-security-public-edge-boundary-custody-enrollment platform-security-public-edge-boundary-legacy-bootstrap \
 && /out/public-edge-trust-bundle-gate /production-trust/trusted-native-response-issuers.json "${FS2_PRODUCTION_NATIVE_TRUST_SHA256}" fs2-serve.nebius.ai/public-edge-native-response-trust/v1 provider-control-plane-native-response-authority \
 && /out/public-edge-trust-bundle-gate /production-trust/trusted-snapshot-issuers.json "${FS2_PRODUCTION_SNAPSHOT_TRUST_SHA256}" fs2-serve.nebius.ai/public-edge-boundary-trust/v1 platform-security-public-edge-boundary-runtime

FROM developer-runtime AS production-runtime
LABEL ai.nebius.fs2.public-edge.production="true" \
      ai.nebius.fs2.public-edge.promotion="owner-enrolled-trust-required"
COPY --from=production-trust-gate /production-trust/trusted-acceptance-issuers.json /usr/local/share/fs2-boundary/trusted-acceptance-issuers.json
COPY --from=production-trust-gate /production-trust/trusted-native-response-issuers.json /usr/local/share/fs2-boundary/trusted-native-response-issuers.json
COPY --from=production-trust-gate /production-trust/trusted-snapshot-issuers.json /usr/local/share/fs2-boundary/trusted-snapshot-issuers.json
