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
const legacyRuntimeBootstrapEnvelopePath = "/var/run/fs2-boundary/acceptance/legacy-runtime-bootstrap-envelope.json"
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
	legacyBootstrap, err := boundary.LoadOptionalLegacyRuntimeBootstrap(
		acceptanceTrustPath, legacyRuntimeBootstrapEnvelopePath, acceptance, time.Now(),
	)
	if err != nil {
		slog.Error("legacy runtime bootstrap rejected", "error", err)
		os.Exit(1)
	}
	runner, err := collector.LoadRunner(collectorConfigPath, nativeTrustPath, snapshotTrustPath, acceptance, legacyBootstrap)
	if err != nil {
		slog.Error("collector configuration rejected", "error", err)
		os.Exit(1)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	refreshInterval := time.Duration(runner.Config.RefreshIntervalSeconds) * time.Second
	var lastWall time.Time
	for {
		now := time.Now().UTC()
		if !lastWall.IsZero() && now.Before(lastWall.Add(-time.Second)) {
			slog.Error("collector wall clock moved backwards; refusing to derive a second cadence branch")
			return
		}
		lastWall = now
		cycle, err := runner.AcceptedCycle(now)
		if err != nil {
			slog.Error("collector cadence derivation rejected", "error", err)
			return
		}
		issuedAt, issueErr := time.Parse(time.RFC3339, cycle.IssuedAt)
		deadlineAt, deadlineErr := time.Parse(time.RFC3339, cycle.DeadlineAt)
		if issueErr != nil || deadlineErr != nil {
			slog.Error("collector cadence timestamps are not canonical")
			return
		}
		if !now.Before(deadlineAt) {
			nextBoundary := issuedAt.Add(refreshInterval)
			timer := time.NewTimer(time.Until(nextBoundary))
			select {
			case <-ctx.Done():
				timer.Stop()
				return
			case <-timer.C:
			}
			continue
		}
		remaining := time.Until(deadlineAt)
		if remaining <= 0 {
			continue
		}
		attempt, cancel := context.WithTimeout(ctx, remaining)
		err = runner.CollectAndInstall(attempt, now)
		cancel()
		if err != nil {
			slog.Error("native authority refresh rejected", "error", err)
		} else {
			slog.Info("native authority snapshot refreshed")
		}
		nextBoundary := issuedAt.Add(refreshInterval)
		timer := time.NewTimer(time.Until(nextBoundary))
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
	}
}
