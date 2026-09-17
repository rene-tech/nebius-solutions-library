package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/boundary"
	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/collector"
)

const acceptanceTrustPath = "/usr/local/share/fs2-boundary/trusted-acceptance-issuers.json"
const acceptanceEnvelopePath = "/var/run/fs2-boundary/acceptance/accepted-boundary-envelope.json"
const collectorConfigPath = "/usr/local/share/fs2-boundary/native-collector.json"
const nativeTrustPath = "/usr/local/share/fs2-boundary/trusted-native-response-issuers.json"
const snapshotTrustPath = "/usr/local/share/fs2-boundary/trusted-snapshot-issuers.json"

func main() {
	if os.Geteuid() != 0 {
		slog.Error("collector requires the separately trusted root writer identity")
		os.Exit(1)
	}
	acceptance, err := boundary.LoadAcceptance(acceptanceTrustPath, acceptanceEnvelopePath, "collector")
	if err != nil {
		slog.Error("collector acceptance rejected", "error", err)
		os.Exit(1)
	}
	runner, err := collector.LoadRunner(collectorConfigPath, nativeTrustPath, snapshotTrustPath, acceptance)
	if err != nil {
		slog.Error("collector configuration rejected", "error", err)
		os.Exit(1)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	refreshInterval := time.Duration(runner.Config.RefreshIntervalSeconds) * time.Second
	collectionDeadline := time.Duration(runner.Config.CollectionDeadlineSeconds) * time.Second
	ticker := time.NewTicker(refreshInterval)
	defer ticker.Stop()
	for {
		attempt, cancel := context.WithTimeout(ctx, collectionDeadline)
		err := runner.CollectAndInstall(attempt, time.Now())
		cancel()
		if err != nil {
			slog.Error("native authority refresh rejected", "error", err)
		} else {
			slog.Info("native authority snapshot refreshed")
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}
