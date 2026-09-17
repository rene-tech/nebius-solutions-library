ARG FS2_GO_BUILDER_IMAGE
FROM ${FS2_GO_BUILDER_IMAGE} AS build
WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -buildvcs=false -ldflags='-s -w' -o /out/public-edge-boundary ./cmd/webhook \
 && CGO_ENABLED=0 GOOS=linux go build -trimpath -buildvcs=false -ldflags='-s -w' -o /out/public-edge-collector ./cmd/collector \
 && CGO_ENABLED=0 GOOS=linux go build -trimpath -buildvcs=false -ldflags='-s -w' -o /out/public-edge-native-authority ./cmd/native-authority

FROM scratch
COPY --from=build /out/public-edge-boundary /usr/local/bin/public-edge-boundary
COPY --from=build /out/public-edge-collector /usr/local/bin/public-edge-collector
COPY --from=build /out/public-edge-native-authority /usr/local/bin/public-edge-native-authority
COPY config/trusted-acceptance-issuers.json /usr/local/share/fs2-boundary/trusted-acceptance-issuers.json
COPY config/native-collector.json /usr/local/share/fs2-boundary/native-collector.json
COPY config/native-authority.json /usr/local/share/fs2-boundary/native-authority.json
COPY config/trusted-native-response-issuers.json /usr/local/share/fs2-boundary/trusted-native-response-issuers.json
COPY config/trusted-snapshot-issuers.json /usr/local/share/fs2-boundary/trusted-snapshot-issuers.json
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/public-edge-boundary"]
