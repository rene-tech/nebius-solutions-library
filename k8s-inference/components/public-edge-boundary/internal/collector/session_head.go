package collector

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"time"
)

const (
	authoritySessionHeadSchema = "fs2-serve.nebius.ai/public-edge-authority-session-head/v2"
	maximumSessionHeadBytes    = 32 * 1024
)

type authoritySessionHeadPayload struct {
	Schema                    string `json:"schema"`
	ClusterID                 string `json:"cluster_id"`
	DeploymentID              string `json:"deployment_id"`
	SourceID                  string `json:"source_id"`
	CycleContractSHA256       string `json:"cycle_contract_sha256"`
	CycleID                   string `json:"cycle_id"`
	CycleIssuedAt             string `json:"cycle_issued_at"`
	CycleDeadlineAt           string `json:"cycle_deadline_at"`
	SessionID                 string `json:"session_id"`
	NextPage                  int    `json:"next_page"`
	NextToken                 string `json:"next_token"`
	Terminal                  bool   `json:"terminal"`
	ResponseBytes             int64  `json:"response_bytes"`
	LastChallenge             string `json:"last_challenge"`
	LastSettlementSHA256      string `json:"last_settlement_sha256"`
	PreviousHeadSHA256        string `json:"previous_head_sha256"`
	UpdatedAt                 string `json:"updated_at"`
}

type authoritySessionHead struct {
	Payload   authoritySessionHeadPayload `json:"payload"`
	Issuer    string                      `json:"issuer"`
	KeyID     string                      `json:"key_id"`
	Algorithm string                      `json:"algorithm"`
	Signature string                      `json:"signature"`
}

func sessionHeadsRoot(config AuthorityConfig, sessionID string) string {
	return filepath.Join(sessionLedgerRoot(config, sessionID), "heads")
}

func sessionHeadPath(config AuthorityConfig, sessionID string, nextPage int) string {
	return filepath.Join(sessionHeadsRoot(config, sessionID), fmt.Sprintf("head-%08d.json", nextPage))
}

func loadAuthoritySessionHead(config AuthorityConfig, sessionID string, expectedSource string, expectedNextPage int, publicKey ed25519.PublicKey) (authoritySession, error) {
	state := authoritySession{sourceID: expectedSource, nextPage: expectedNextPage, settlements: map[int]AuthoritySettlement{}}
	if !canonicalNonce(sessionID) || expectedNextPage < 0 || expectedNextPage > maximumAuthoritySessionPages {
		return state, errors.New("authority session-head lookup identity is invalid")
	}
	if expectedNextPage == 0 {
		return state, nil
	}
	raw, err := readRootRegular(sessionHeadPath(config, sessionID, expectedNextPage), maximumSessionHeadBytes)
	if err != nil {
		return state, err
	}
	var head authoritySessionHead
	if err := canonicalJSON(raw, &head); err != nil {
		return state, err
	}
	payloadRaw, err := json.Marshal(head.Payload)
	if err != nil || head.Payload.Schema != authoritySessionHeadSchema || head.Payload.ClusterID != config.ClusterID ||
			head.Payload.DeploymentID != config.DeploymentID || head.Payload.SourceID != expectedSource ||
			!isDigest(head.Payload.CycleContractSHA256) || !isDigest(head.Payload.CycleID) ||
		head.Payload.SessionID != sessionID || head.Payload.NextPage != expectedNextPage ||
		head.Payload.ResponseBytes < 0 || !canonicalNonce(head.Payload.LastChallenge) || !isDigest(head.Payload.LastSettlementSHA256) ||
		head.Payload.Terminal == (head.Payload.NextToken != "") || head.Issuer != config.SigningIssuer || head.KeyID != config.SigningKeyID ||
		head.Algorithm != "ed25519" {
		return state, errors.New("authority session head has an invalid signed projection")
	}
	updatedAt, err := time.Parse(time.RFC3339, head.Payload.UpdatedAt)
	cycleIssuedAt, issuedErr := time.Parse(time.RFC3339, head.Payload.CycleIssuedAt)
	cycleDeadlineAt, deadlineErr := time.Parse(time.RFC3339, head.Payload.CycleDeadlineAt)
	if err != nil || issuedErr != nil || deadlineErr != nil || updatedAt.Nanosecond() != 0 ||
		cycleIssuedAt.Nanosecond() != 0 || cycleDeadlineAt.Nanosecond() != 0 ||
		cycleDeadlineAt.Sub(cycleIssuedAt) != time.Duration(config.CollectorCollectionDeadlineSeconds)*time.Second ||
		updatedAt.Before(cycleIssuedAt) || !updatedAt.Before(cycleDeadlineAt) {
		return state, errors.New("authority session head timestamp is not canonical")
	}
	signature, err := base64.RawURLEncoding.DecodeString(head.Signature)
	if err != nil || len(signature) != ed25519.SignatureSize || base64.RawURLEncoding.EncodeToString(signature) != head.Signature ||
		!ed25519.Verify(publicKey, ledgerSignatureMessage(authoritySessionHeadSchema, head.Issuer, head.KeyID, payloadRaw), signature) {
		return state, errors.New("authority session head signature is invalid")
	}
	if expectedNextPage == 1 {
		if head.Payload.PreviousHeadSHA256 != "" {
			return state, errors.New("first authority session head has an unexpected predecessor")
		}
	} else {
		previousRaw, err := readRootRegular(sessionHeadPath(config, sessionID, expectedNextPage-1), maximumSessionHeadBytes)
		if err != nil || digest(previousRaw) != head.Payload.PreviousHeadSHA256 {
			return state, errors.New("authority session head does not extend its retained predecessor")
		}
	}
	settlementPath := filepath.Join(sessionLedgerRoot(config, sessionID), fmt.Sprintf("page-%08d.json", expectedNextPage-1))
	settlementRaw, err := readRootRegular(settlementPath, maximumAuthorityLedgerRecordBytes)
	if err != nil || digest(settlementRaw) != head.Payload.LastSettlementSHA256 {
		return state, errors.New("authority session head does not bind its last settlement bytes")
	}
	var settlement AuthoritySettlement
	if err := canonicalJSON(settlementRaw, &settlement); err != nil || settlement.SourceID != expectedSource ||
			settlement.SessionID != sessionID || settlement.PageIndex != expectedNextPage-1 || settlement.Challenge != head.Payload.LastChallenge ||
			settlement.CycleContractSHA256 != head.Payload.CycleContractSHA256 || settlement.CycleID != head.Payload.CycleID || settlement.CycleIssuedAt != head.Payload.CycleIssuedAt ||
			settlement.CycleDeadlineAt != head.Payload.CycleDeadlineAt ||
		settlement.NextToken != head.Payload.NextToken || settlement.Terminal != head.Payload.Terminal {
		return state, errors.New("authority session head differs from its last settlement identity")
	}
	state.nextToken = head.Payload.NextToken
	state.cycleContractSHA256 = head.Payload.CycleContractSHA256
	state.cycleID = head.Payload.CycleID
	state.cycleIssuedAt = head.Payload.CycleIssuedAt
	state.cycleDeadlineAt = head.Payload.CycleDeadlineAt
	state.terminal = head.Payload.Terminal
	state.responseBytes = head.Payload.ResponseBytes
	state.previousSettlementSHA256 = head.Payload.LastSettlementSHA256
	state.headSHA256 = digest(raw)
	state.settlements[settlement.PageIndex] = settlement
	return state, nil
}

func (a *Authority) ensureSessionHead(settlement AuthoritySettlement) (authoritySession, []byte, error) {
	previous, err := loadAuthoritySessionHead(a.Config, settlement.SessionID, settlement.SourceID, settlement.PageIndex, a.publicKey)
	if err != nil {
		return authoritySession{}, nil, err
	}
	if settlement.PageIndex > 0 && (previous.cycleContractSHA256 != settlement.CycleContractSHA256 ||
		previous.cycleID != settlement.CycleID || previous.cycleIssuedAt != settlement.CycleIssuedAt ||
		previous.cycleDeadlineAt != settlement.CycleDeadlineAt) {
		return authoritySession{}, nil, errors.New("authority session head changed its accepted collection cycle")
	}
	settlementPath := filepath.Join(sessionLedgerRoot(a.Config, settlement.SessionID), fmt.Sprintf("page-%08d.json", settlement.PageIndex))
	settlementRaw, err := readRootRegular(settlementPath, maximumAuthorityLedgerRecordBytes)
	if err != nil {
		return authoritySession{}, nil, err
	}
	envelopeRaw, err := decodeSettlementEnvelope(settlement)
	if err != nil {
		return authoritySession{}, nil, err
	}
	responseBytes, err := validateRetainedEnvelope(a.Config, settlement, envelopeRaw, a.publicKey)
	if err != nil {
		return authoritySession{}, nil, err
	}
	source := a.sources[settlement.SourceID]
	if previous.responseBytes > source.MaximumTotalBytes-responseBytes {
		return authoritySession{}, nil, errors.New("authority session head exceeds its enrolled total response bound")
	}
	payload := authoritySessionHeadPayload{
		Schema:               authoritySessionHeadSchema,
		ClusterID:            a.Config.ClusterID,
		DeploymentID:         a.Config.DeploymentID,
			SourceID:             settlement.SourceID,
			CycleContractSHA256: settlement.CycleContractSHA256,
			CycleID:              settlement.CycleID,
			CycleIssuedAt:        settlement.CycleIssuedAt,
			CycleDeadlineAt:      settlement.CycleDeadlineAt,
		SessionID:            settlement.SessionID,
		NextPage:             settlement.PageIndex + 1,
		NextToken:            settlement.NextToken,
		Terminal:             settlement.Terminal,
		ResponseBytes:        previous.responseBytes + responseBytes,
		LastChallenge:        settlement.Challenge,
		LastSettlementSHA256: digest(settlementRaw),
		PreviousHeadSHA256:   previous.headSHA256,
		UpdatedAt:            settlement.SettledAt,
	}
	payloadRaw, err := json.Marshal(payload)
	if err != nil {
		return authoritySession{}, nil, err
	}
	head := authoritySessionHead{
		Payload:   payload,
		Issuer:    a.Config.SigningIssuer,
		KeyID:     a.Config.SigningKeyID,
		Algorithm: "ed25519",
		Signature: base64.RawURLEncoding.EncodeToString(ed25519.Sign(a.privateKey, ledgerSignatureMessage(authoritySessionHeadSchema, a.Config.SigningIssuer, a.Config.SigningKeyID, payloadRaw))),
	}
	headRaw, err := json.Marshal(head)
	if err != nil {
		return authoritySession{}, nil, err
	}
	headPath := sessionHeadPath(a.Config, settlement.SessionID, settlement.PageIndex+1)
	if err := a.publishLedgerObject("session-head", settlement.SessionID, settlement.Challenge, settlement.PageIndex, headPath, headRaw); err != nil {
		return authoritySession{}, nil, err
	}
	state, err := loadAuthoritySessionHead(a.Config, settlement.SessionID, settlement.SourceID, settlement.PageIndex+1, a.publicKey)
	return state, headRaw, err
}
