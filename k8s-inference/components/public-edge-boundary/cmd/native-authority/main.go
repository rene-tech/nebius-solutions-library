package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"

	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/boundary"
	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/collector"
)

const acceptanceTrustPath = "/usr/local/share/fs2-boundary/trusted-acceptance-issuers.json"
const acceptanceEnvelopePath = "/var/run/fs2-boundary/acceptance/accepted-boundary-envelope.json"
const authorityConfigPath = "/usr/local/share/fs2-boundary/native-authority.json"

func main() {
	acceptance, err := boundary.LoadAcceptance(acceptanceTrustPath, acceptanceEnvelopePath, "native-authority")
	if err != nil {
		slog.Error("native authority acceptance rejected", "error", err)
		os.Exit(1)
	}
	authority, err := collector.LoadAuthority(authorityConfigPath, acceptance)
	if err != nil {
		slog.Error("native authority configuration rejected", "error", err)
		os.Exit(1)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if err := authority.Serve(ctx); err != nil && !errors.Is(err, http.ErrServerClosed) {
		slog.Error("native authority stopped", "error", err)
		os.Exit(1)
	}
}
