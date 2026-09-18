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
	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/snapshotauthority"
)

const acceptanceTrustPath = "/usr/local/share/fs2-boundary/trusted-acceptance-issuers.json"
const acceptanceEnvelopePath = "/var/run/fs2-boundary/acceptance/accepted-boundary-envelope.json"
const snapshotAuthorityConfigPath = "/var/run/fs2-boundary/config/snapshot-authority.json"

func main() {
	mode := "authority"
	if len(os.Args) == 2 { mode = os.Args[1] } else if len(os.Args) != 1 { slog.Error("snapshot authority mode is invalid"); os.Exit(1) }
	acceptance, err := boundary.LoadAcceptance(acceptanceTrustPath, acceptanceEnvelopePath, "snapshot-authority")
	if err != nil { slog.Error("snapshot authority acceptance rejected", "error", err); os.Exit(1) }
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if mode == "settlement-custodian" {
		custodian, loadErr := snapshotauthority.LoadSettlementCustodian(snapshotAuthorityConfigPath, acceptance)
		if loadErr != nil { slog.Error("snapshot settlement custodian configuration rejected", "error", loadErr); os.Exit(1) }
		if serveErr := custodian.Serve(ctx); serveErr != nil { slog.Error("snapshot settlement custodian stopped", "error", serveErr); os.Exit(1) }
		return
	}
	if mode != "authority" { slog.Error("snapshot authority mode is unsupported", "mode", mode); os.Exit(1) }
	authority, err := snapshotauthority.LoadAuthority(snapshotAuthorityConfigPath, acceptance)
	if err != nil { slog.Error("snapshot authority configuration rejected", "error", err); os.Exit(1) }
	if err := authority.Serve(ctx); err != nil && !errors.Is(err, http.ErrServerClosed) { slog.Error("snapshot authority stopped", "error", err); os.Exit(1) }
}
