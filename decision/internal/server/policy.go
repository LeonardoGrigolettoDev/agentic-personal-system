package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"aios/decision/internal/decide"
	"aios/decision/internal/ledger"
	"aios/decision/internal/policy"
	"aios/decision/internal/store"
)

const maxPolicyBody = 256 << 10

// Ledger is the persistence the policy endpoints need (implemented by ledger.DB).
type Ledger interface {
	GetRun(ctx context.Context, sessionID string) (*ledger.Run, error)
	CreateRun(ctx context.Context, r ledger.Run) (*ledger.Run, error)
	RecordUsage(ctx context.Context, u ledger.UsageRow) (bool, *ledger.Run, error)
	GlobalSpend(ctx context.Context) ([]policy.Spend, error)
	Stats(ctx context.Context, taskType string) ([]policy.Stat, error)
	ApplyGate(ctx context.Context, run *ledger.Run, g policy.Gate, nextTier int, failed bool, evidence any, decidedBy string) (*ledger.Run, error)
	FinishRun(ctx context.Context, sessionID, status, taskType string) (*ledger.Run, error)
	CreateApproval(ctx context.Context, run *ledger.Run, kind string, request map[string]any) (string, bool, error)
	ResolveApproval(ctx context.Context, id string, approve bool, by string, tierOf func(string) int) (*ledger.Approval, error)
	HasApproval(ctx context.Context, sessionID, model string) (bool, error)
	ListApprovals(ctx context.Context, status string) ([]ledger.Approval, error)
	Report(ctx context.Context, name string) ([]map[string]any, error)
}

func (s *Server) policyRoutes(mux *http.ServeMux) {
	auth := func(h http.HandlerFunc) http.Handler { return RequireBearer(s.APIKey, h) }
	mux.Handle("POST /v1/route", auth(s.route))
	mux.Handle("POST /v1/models/resolve", auth(s.resolve))
	mux.Handle("POST /v1/usage", auth(s.usage))
	mux.Handle("POST /v1/budget/check", auth(s.budgetCheck))
	mux.Handle("POST /v1/gate", auth(s.gate))
	mux.Handle("GET /v1/runs/{session_id}", auth(s.getRun))
	mux.Handle("POST /v1/runs/{session_id}/finish", auth(s.finish))
	mux.Handle("GET /v1/approvals", auth(s.listApprovals))
	mux.Handle("POST /v1/approvals/{id}", auth(s.resolveApproval))
	mux.Handle("GET /v1/reports/{name}", auth(s.report))
	mux.Handle("GET /v1/policy", auth(s.describePolicy))
}

func (s *Server) ready() bool { return s.Policy != nil && s.Ledger != nil }

func decodeBody(w http.ResponseWriter, r *http.Request, v any) bool {
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxPolicyBody))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return false
	}
	return true
}

func validSession(id string) bool {
	return id != "" && len(id) <= 200 && !strings.ContainsAny(id, "\r\n\x00")
}

// ---------------------------------------------------------------- route

type routeRequest struct {
	SessionID       string     `json:"session_id"`
	TaskID          string     `json:"task_id,omitempty"`
	ParentSessionID string     `json:"parent_session_id,omitempty"`
	Text            string     `json:"text"`
	Agent           string     `json:"agent,omitempty"`
	Tenant          string     `json:"tenant,omitempty"`
	Domain          string     `json:"domain,omitempty"`
	TaskType        string     `json:"task_type,omitempty"`
	Complexity      string     `json:"complexity,omitempty"`
	Model           string     `json:"model,omitempty"` // pinned start model (bench model comparisons)
	Deadline        *time.Time `json:"deadline,omitempty"`
}

type routeResponse struct {
	*ledger.Run
	NeedsResearch     bool           `json:"needs_research"`
	NeedsConfirmation bool           `json:"needs_confirmation"`
	Skills            []string       `json:"skills,omitempty"`
	Validator         string         `json:"validator,omitempty"`
	TokenBudget       any            `json:"token_budget,omitempty"`
	Budget            policy.Budget  `json:"budget"`
	Existing          bool           `json:"existing"`
	DecisionTrace     []decide.Step  `json:"decision_trace,omitempty"`
	Classified        map[string]any `json:"classified,omitempty"`
}

var tenantCriteria = map[string]string{
	"nitro":   "trabalho na empresa Nitro: apps, clientes, código e projetos do trabalho",
	"pessoal": "vida pessoal: estudos, finanças pessoais, projetos pessoais, rotina, saúde",
	"shared":  "geral, sem relação com trabalho ou vida pessoal específica",
}

// tenantOptions limits the tenant question to what the agent may access.
func tenantOptions(a policy.Agent) map[string]string {
	opts := map[string]string{}
	for _, t := range a.AllowedTenants {
		if d, ok := tenantCriteria[t]; ok {
			opts[t] = d
		}
	}
	if len(opts) == 0 {
		return tenantCriteria
	}
	return opts
}

var domainCriteria = map[string]string{
	"chief":       "coordenação entre domínios, agenda, pedidos gerais ou que não se encaixam em outro domínio",
	"engineering": "programação, código, bugs, testes, deploy, infraestrutura, repositórios",
	"finance":     "finanças, planilhas, gastos, faturas, orçamento, investimentos, impostos",
	"projects":    "status, roadmap, decisões e próximos passos de projetos e da empresa",
	"personal":    "organização pessoal, rotina, compromissos, saúde, casa",
	"learning":    "estudos, cursos, aulas, leitura, metas de aprendizado",
}

func (s *Server) route(w http.ResponseWriter, r *http.Request) {
	if !s.ready() {
		writeError(w, http.StatusServiceUnavailable, "policy/ledger not configured")
		return
	}
	var req routeRequest
	if !decodeBody(w, r, &req) {
		return
	}
	if !validSession(req.SessionID) {
		writeError(w, http.StatusBadRequest, "session_id is required (<=200 chars)")
		return
	}
	if err := s.validateRouteHints(req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	ctx := r.Context()
	if run, err := s.Ledger.GetRun(ctx, req.SessionID); err == nil {
		writeJSON(w, http.StatusOK, s.routeView(ctx, run, nil, nil, true))
		return
	} else if !errors.Is(err, ledger.ErrNotFound) {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return
	}

	classified, trace, flags := s.classify(ctx, req)
	agent := s.Policy.Agent(req.Agent)
	if req.Agent == "" {
		agent = s.Policy.Agent(s.Policy.DomainAgent[classified["domain"].(string)])
	}
	taskType := classified["task_type"].(string)
	complexity := classified["complexity"].(string)
	stats, err := s.Ledger.Stats(ctx, taskType)
	if err != nil {
		s.Log.WarnContext(ctx, "learned stats unavailable", "err", err)
	}
	choice := s.Policy.StartModel(agent, taskType, complexity, stats)
	if req.Model != "" {
		if tier := s.Policy.Tier(req.Model); agent.MaxTier > 0 && tier > agent.MaxTier {
			writeError(w, http.StatusBadRequest, fmt.Sprintf("model %q (tier %d) is above agent %q max_tier %d",
				req.Model, tier, agent.Slug, agent.MaxTier))
			return
		}
		choice = policy.Choice{Model: req.Model, Tier: s.Policy.Tier(req.Model), Reason: "pinned by caller"}
	}
	run, err := s.Ledger.CreateRun(ctx, ledger.Run{
		SessionID: req.SessionID, Agent: agent.Slug, Tenant: classified["tenant"].(string), Domain: classified["domain"].(string),
		TaskType: taskType, Complexity: complexity, HermesTaskID: req.TaskID, ParentSessionID: req.ParentSessionID,
		Model: choice.Model, Tier: choice.Tier, RouteReason: choice.Reason, MaxCostUSD: agent.MaxCostPerRun,
		MaxTokens: agent.TokenBudget.Total, MaxIterations: agent.MaxIterations, Deadline: req.Deadline, Summary: req.Text,
	})
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	view := s.routeView(ctx, run, trace, classified, false)
	view.NeedsResearch, view.NeedsConfirmation = flags[0], flags[1]
	s.Log.InfoContext(ctx, "route", "session_id", run.SessionID, "agent", run.Agent, "task_type", run.TaskType,
		"complexity", run.Complexity, "model", run.Model, "reason", run.RouteReason)
	writeJSON(w, http.StatusOK, view)
}

func (s *Server) validateRouteHints(req routeRequest) error {
	check := func(field, v string, allowed []string) error {
		if v != "" && !contains(allowed, v) {
			return fmt.Errorf("%s %q must be one of %v", field, v, allowed)
		}
		return nil
	}
	return errors.Join(
		check("domain", req.Domain, policy.Domains),
		check("tenant", req.Tenant, policy.Tenants),
		check("complexity", req.Complexity, policy.Complexities),
		check("task_type", req.TaskType, s.Policy.TaskTypeNames()),
		func() error {
			if _, ok := s.Policy.Models[req.Model]; req.Model != "" && !ok {
				return fmt.Errorf("unknown model %q (not in routing.yaml models)", req.Model)
			}
			return nil
		}(),
		func() error {
			if _, ok := s.Policy.Agents[req.Agent]; req.Agent != "" && !ok {
				return fmt.Errorf("unknown agent %q", req.Agent)
			}
			return nil
		}(),
	)
}

// classify asks the decision cascade only what the caller did not already provide.
// Unanswerable questions fall back to safe defaults (agent's domain, domain's task type, medium).
func (s *Server) classify(ctx context.Context, req routeRequest) (map[string]any, []decide.Step, [2]bool) {
	out := map[string]any{"domain": req.Domain, "task_type": req.TaskType, "complexity": req.Complexity, "tenant": req.Tenant}
	if req.Domain == "" && req.Agent != "" && req.Agent != "chief" {
		out["domain"] = s.Policy.Agent(req.Agent).Domain
	}
	var qs []decide.Question
	if req.Tenant == "" {
		opts := tenantOptions(s.Policy.Agent(req.Agent))
		if len(opts) == 1 {
			for t := range opts {
				out["tenant"] = t
			}
		} else {
			qs = append(qs, decide.Question{Name: "tenant", Type: decide.Choice, Criteria: opts,
				Instructions: "A quem pertence este pedido: trabalho (Nitro), vida pessoal, ou é geral?"})
		}
	}
	if out["domain"] == "" {
		qs = append(qs, decide.Question{Name: "domain", Type: decide.Choice, Criteria: domainCriteria,
			Instructions: "Qual domínio de agente deve tratar este pedido?"})
	}
	if req.TaskType == "" {
		crit := map[string]string{}
		for _, n := range s.Policy.TaskTypeNames() {
			crit[n] = s.Policy.TaskTypes[n].Description
		}
		qs = append(qs, decide.Question{Name: "task_type", Type: decide.Choice, Criteria: crit,
			Instructions: "Que tipo de tarefa é este pedido?"})
	}
	if req.Complexity == "" {
		qs = append(qs, decide.Question{Name: "complexity", Type: decide.Score, Levels: policy.Complexities,
			Instructions: "Quão complexa é a tarefa (esforço, risco, quantidade de passos)?"})
	}
	qs = append(qs,
		decide.Question{Name: "needs_research", Type: decide.Binary,
			Instructions: "A tarefa exige pesquisa externa (web/documentação) antes de executar?"},
		decide.Question{Name: "needs_confirmation", Type: decide.Binary,
			Instructions: "A tarefa envolve ação destrutiva, gasto de dinheiro ou envio externo que exige confirmação humana?"},
	)

	var flags [2]bool
	var trace []decide.Step
	if s.Engine != nil && strings.TrimSpace(req.Text) != "" {
		dreq := decide.Request{State: map[string]any{"text": req.Text, "agent": req.Agent, "tenant": req.Tenant}, Questions: qs}
		answers, tr, err := s.Engine.DecideTrace(ctx, dreq)
		trace = tr
		if err != nil {
			s.Log.WarnContext(ctx, "route classification failed", "err", err)
		}
		for _, a := range answers {
			if a.NeedsHuman || a.Refused {
				continue
			}
			switch a.Name {
			case "needs_research":
				flags[0] = a.Choice == "true"
			case "needs_confirmation":
				flags[1] = a.Choice == "true"
			default:
				out[a.Name] = a.Choice
			}
		}
		if len(answers) > 0 {
			backend, minConf, needsHuman := decide.Summary(answers)
			s.persist(store.DecisionRow{RequestID: newID(), RequestHash: decide.RequestHash(dreq), Backend: backend,
				State: dreq.State, Questions: qs, Answers: answers, MinConfidence: minConf, NeedsHuman: needsHuman})
		}
	}
	if out["domain"] == "" {
		out["domain"] = "chief"
	}
	if out["tenant"] == "" {
		out["tenant"] = "shared" // least privilege: only global knowledge until the tenant is known
	}
	if out["task_type"] == "" {
		out["task_type"] = s.Policy.DomainDefaultTaskType[out["domain"].(string)]
	}
	if out["complexity"] == "" {
		out["complexity"] = "medium"
	}
	return out, trace, flags
}

func (s *Server) routeView(ctx context.Context, run *ledger.Run, trace []decide.Step, classified map[string]any, existing bool) routeResponse {
	tt := s.Policy.TaskTypes[run.TaskType]
	return routeResponse{Run: run, Skills: tt.Skills, Validator: tt.Validator, Budget: s.budgetOf(ctx, run),
		TokenBudget: s.Policy.Agent(run.Agent).TokenBudget, Existing: existing, DecisionTrace: trace, Classified: classified}
}

func (s *Server) budgetOf(ctx context.Context, run *ledger.Run) policy.Budget {
	spend, err := s.Ledger.GlobalSpend(ctx)
	if err != nil {
		s.Log.WarnContext(ctx, "global spend unavailable", "err", err)
	}
	return s.Policy.CheckBudget(run.Usage(), run.Limits(), spend, time.Now())
}

// ---------------------------------------------------------------- resolve (hot path: every LLM call)

func (s *Server) resolve(w http.ResponseWriter, r *http.Request) {
	if !s.ready() {
		writeError(w, http.StatusServiceUnavailable, "policy/ledger not configured")
		return
	}
	var req struct {
		SessionID      string `json:"session_id"`
		RequestedModel string `json:"requested_model"`
	}
	if !decodeBody(w, r, &req) {
		return
	}
	ctx := r.Context()
	model, source := req.RequestedModel, "requested"
	run, err := s.Ledger.GetRun(ctx, req.SessionID)
	switch {
	case err == nil:
		model, source = run.Model, "run"
	case !errors.Is(err, ledger.ErrNotFound):
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	approved := false
	if m, ok := s.Policy.Models[model]; ok && m.RequiresApproval && run != nil {
		approved, _ = s.Ledger.HasApproval(ctx, run.SessionID, model)
	}
	resolved, why := s.Policy.Resolve(model, approved)
	writeJSON(w, http.StatusOK, map[string]any{"model": resolved, "tier": s.Policy.Tier(resolved), "source": source, "note": why})
}

// ---------------------------------------------------------------- usage / budget

type usageRequest struct {
	SessionID    string   `json:"session_id"`
	TaskID       string   `json:"task_id,omitempty"`
	Agent        string   `json:"agent,omitempty"`
	Model        string   `json:"model"`
	InputTokens  int      `json:"input_tokens"`
	OutputTokens int      `json:"output_tokens"`
	CacheTokens  int      `json:"cache_read_tokens"`
	APIRequestID string   `json:"api_request_id"`
	Purpose      string   `json:"purpose,omitempty"`
	Success      *bool    `json:"success,omitempty"`
	LatencyMS    int      `json:"latency_ms,omitempty"`
	Skills       []string `json:"skills,omitempty"`
}

func (s *Server) usage(w http.ResponseWriter, r *http.Request) {
	if !s.ready() {
		writeError(w, http.StatusServiceUnavailable, "policy/ledger not configured")
		return
	}
	var req usageRequest
	if !decodeBody(w, r, &req) {
		return
	}
	if !validSession(req.SessionID) || req.InputTokens < 0 || req.OutputTokens < 0 || req.CacheTokens < 0 {
		writeError(w, http.StatusBadRequest, "session_id and non-negative token counts are required")
		return
	}
	ctx := r.Context()
	run, err := s.ensureRun(ctx, req.SessionID, req.Agent, req.Model, req.TaskID)
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	model := req.Model
	if _, known := s.Policy.Models[model]; !known {
		model = run.Model // providers may echo their own model id; bill at the alias we routed to
	}
	success := req.Success == nil || *req.Success
	cost := s.Policy.Cost(model, req.InputTokens, req.OutputTokens, req.CacheTokens)
	applied, run, err := s.Ledger.RecordUsage(ctx, ledger.UsageRow{
		SessionID: req.SessionID, RequestKey: req.APIRequestID, Model: model, Tier: s.Policy.Tier(model), Purpose: req.Purpose,
		InputTokens: req.InputTokens, OutputTokens: req.OutputTokens, CacheTokens: req.CacheTokens, CostUSD: cost,
		LatencyMS: req.LatencyMS, Success: success, Metadata: map[string]any{"skills": nonNil(req.Skills)},
	})
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"cost_usd": cost, "applied": applied, "run": run, "budget": s.budgetOf(ctx, run)})
}

// ensureRun returns the session's run, creating a default one for sessions that were never routed
// (interactive chats, auxiliary calls) so their spend is still attributed and budgeted.
func (s *Server) ensureRun(ctx context.Context, sessionID, agentSlug, model, taskID string) (*ledger.Run, error) {
	run, err := s.Ledger.GetRun(ctx, sessionID)
	if err == nil {
		return run, nil
	}
	if !errors.Is(err, ledger.ErrNotFound) {
		return nil, err
	}
	agent := s.Policy.Agent(agentSlug)
	if _, ok := s.Policy.Models[model]; !ok {
		model = agent.DefaultModel
	}
	return s.Ledger.CreateRun(ctx, ledger.Run{SessionID: sessionID, Agent: agent.Slug, Domain: agent.Domain,
		HermesTaskID: taskID, Model: model, Tier: s.Policy.Tier(model), RouteReason: "unrouted session (default run)",
		MaxCostUSD: agent.MaxCostPerRun, MaxTokens: agent.TokenBudget.Total, MaxIterations: agent.MaxIterations})
}

func (s *Server) budgetCheck(w http.ResponseWriter, r *http.Request) {
	run, ok := s.runFromBody(w, r)
	if !ok {
		return
	}
	writeJSON(w, http.StatusOK, s.budgetOf(r.Context(), run))
}

func (s *Server) runFromBody(w http.ResponseWriter, r *http.Request) (*ledger.Run, bool) {
	if !s.ready() {
		writeError(w, http.StatusServiceUnavailable, "policy/ledger not configured")
		return nil, false
	}
	var req struct {
		SessionID string `json:"session_id"`
	}
	if !decodeBody(w, r, &req) {
		return nil, false
	}
	return s.lookupRun(w, r.Context(), req.SessionID)
}

func (s *Server) lookupRun(w http.ResponseWriter, ctx context.Context, sessionID string) (*ledger.Run, bool) {
	run, err := s.Ledger.GetRun(ctx, sessionID)
	if errors.Is(err, ledger.ErrNotFound) {
		writeError(w, http.StatusNotFound, "no run for session "+sessionID)
		return nil, false
	}
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return nil, false
	}
	return run, true
}

// ---------------------------------------------------------------- gate (escalation)

type gateRequest struct {
	SessionID string          `json:"session_id"`
	Agent     string          `json:"agent,omitempty"`
	Evidence  policy.Evidence `json:"evidence"`
}

type gateResponse struct {
	policy.Gate
	Message    string      `json:"message"`
	DecidedBy  string      `json:"decided_by"`
	ApprovalID string      `json:"approval_id,omitempty"`
	Run        *ledger.Run `json:"run"`
}

func (s *Server) gate(w http.ResponseWriter, r *http.Request) {
	if !s.ready() {
		writeError(w, http.StatusServiceUnavailable, "policy/ledger not configured")
		return
	}
	var req gateRequest
	if !decodeBody(w, r, &req) {
		return
	}
	if !validSession(req.SessionID) || !contains([]string{"pass", "fail", "none", ""}, req.Evidence.Validation) {
		writeError(w, http.StatusBadRequest, "session_id required; evidence.validation must be pass|fail|none")
		return
	}
	if req.Evidence.Tests != nil && len(req.Evidence.Tests.OutputTail) > 8000 {
		req.Evidence.Tests.OutputTail = req.Evidence.Tests.OutputTail[len(req.Evidence.Tests.OutputTail)-8000:]
	}
	ctx := r.Context()
	run, err := s.ensureRun(ctx, req.SessionID, req.Agent, "", "")
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	agent := s.Policy.Agent(run.Agent)
	state := policy.RunState{Model: run.Model, ConsecutiveFailures: run.ConsecutiveFailures, Iterations: run.Iterations,
		MaxIterations: run.MaxIterations, MaxTier: agent.MaxTier, Budget: s.budgetOf(ctx, run)}
	g := s.Policy.Decide(state, req.Evidence)
	decidedBy := "policy"
	if len(g.Ask) > 0 {
		g, decidedBy = s.askNextStep(ctx, run, state, g, req.Evidence)
	}
	failed := req.Evidence.Validation == "fail" || (req.Evidence.Tests != nil && req.Evidence.Tests.ExitCode != 0)

	resp := gateResponse{Gate: g, DecidedBy: decidedBy}
	if g.Action == policy.ActionAskHuman {
		id, created, err := s.Ledger.CreateApproval(ctx, run, "tier", map[string]any{"model": g.NextModel, "tier": g.Tier,
			"reasons": g.Reasons, "from_model": run.Model})
		if err != nil {
			s.Log.ErrorContext(ctx, "create approval failed", "err", err)
		}
		resp.ApprovalID = id
		if created {
			s.Notifier.Send("Aprovação necessária ("+run.Agent+")", fmt.Sprintf(
				"Tarefa: %s\nEscalonar %s -> %s.\nMotivos: %s\nAprovar: make approve id=%s  (rejeitar: make approve id=%s no=1)",
				truncate(run.Summary, 300), run.Model, g.NextModel, strings.Join(g.Reasons, "; "), id, id), true)
		}
	}
	if g.Action == policy.ActionFail {
		s.Notifier.Send("Tarefa interrompida ("+run.Agent+")", fmt.Sprintf("Tarefa: %s\nMotivos: %s\nCusto: $%.4f",
			truncate(run.Summary, 300), strings.Join(g.Reasons, "; "), run.CostUSD), false)
	}
	run, err = s.Ledger.ApplyGate(ctx, run, g, s.Policy.Tier(g.NextModel), failed, req.Evidence, decidedBy)
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	resp.Run = run
	resp.Message = gateMessage(g, req.Evidence, resp.ApprovalID)
	s.Log.InfoContext(ctx, "gate", "session_id", run.SessionID, "action", g.Action, "next_model", g.NextModel,
		"decided_by", decidedBy, "reasons", g.Reasons)
	writeJSON(w, http.StatusOK, resp)
}

// askNextStep consults the typed-decision cascade (rules -> local -> Jev) when no guard applies.
func (s *Server) askNextStep(ctx context.Context, run *ledger.Run, st policy.RunState, g policy.Gate, ev policy.Evidence) (policy.Gate, string) {
	if s.Engine == nil {
		return s.Policy.Apply(st, g, "", ""), "default"
	}
	crit := map[string]string{
		policy.ActionDone:     "a tarefa está concluída e verificada; entregar o resultado",
		policy.ActionRepair:   "há erros corrigíveis no mesmo modelo; diagnosticar e corrigir",
		policy.ActionEscalate: "o modelo atual não está conseguindo; trocar por um modelo mais forte",
	}
	opts := map[string]string{}
	for _, a := range g.Ask {
		opts[a] = crit[a]
	}
	req := decide.Request{
		State: map[string]any{"task": run.Summary, "task_type": run.TaskType, "model": run.Model,
			"iterations": run.Iterations, "repairs": run.Repairs, "consecutive_failures": run.ConsecutiveFailures,
			"evidence": ev},
		Questions: []decide.Question{{Name: "next_step", Type: decide.Choice, Criteria: opts,
			Instructions: "Dado o estado e a evidência de validação, qual deve ser o próximo passo?"}},
	}
	answers, _, err := s.Engine.DecideTrace(ctx, req)
	if err != nil || len(answers) == 0 || answers[0].NeedsHuman || answers[0].Refused {
		return s.Policy.Apply(st, g, "", ""), "default"
	}
	a := answers[0]
	return s.Policy.Apply(st, g, a.Choice, fmt.Sprintf("%s chose %s (confidence %.2f)", a.Backend, a.Choice, a.Confidence)), a.Backend
}

func gateMessage(g policy.Gate, ev policy.Evidence, approvalID string) string {
	detail := strings.Join(ev.Failures, "; ")
	if ev.Tests != nil && ev.Tests.ExitCode != 0 {
		detail = strings.TrimSpace(detail + "\nSaída dos testes (fim):\n" + ev.Tests.OutputTail)
	}
	switch g.Action {
	case policy.ActionRepair:
		return "A validação falhou. Diagnostique a causa raiz antes de editar, corrija e rode a validação de novo.\n" + detail
	case policy.ActionEscalate:
		return fmt.Sprintf("Escalonado para %s (tier %d): %s. Revise a abordagem do zero antes de continuar.\n%s",
			g.NextModel, g.Tier, strings.Join(g.Reasons, "; "), detail)
	case policy.ActionAskHuman:
		return fmt.Sprintf("Pare e peça aprovação humana (approval %s): %s. Resuma o que foi tentado e o que falta.",
			approvalID, strings.Join(g.Reasons, "; "))
	case policy.ActionFail:
		return "Pare: " + strings.Join(g.Reasons, "; ") + ". Entregue um relatório do que foi feito e do que falta."
	}
	return "Concluído: " + strings.Join(g.Reasons, "; ")
}

// ---------------------------------------------------------------- runs, approvals, reports

func (s *Server) getRun(w http.ResponseWriter, r *http.Request) {
	if !s.ready() {
		writeError(w, http.StatusServiceUnavailable, "policy/ledger not configured")
		return
	}
	run, ok := s.lookupRun(w, r.Context(), r.PathValue("session_id"))
	if !ok {
		return
	}
	writeJSON(w, http.StatusOK, s.routeView(r.Context(), run, nil, nil, true))
}

func (s *Server) finish(w http.ResponseWriter, r *http.Request) {
	if !s.ready() {
		writeError(w, http.StatusServiceUnavailable, "policy/ledger not configured")
		return
	}
	var req struct {
		Status   string `json:"status"`
		TaskType string `json:"task_type,omitempty"`
	}
	if !decodeBody(w, r, &req) {
		return
	}
	if !contains([]string{"succeeded", "failed", "cancelled"}, req.Status) ||
		(req.TaskType != "" && !contains(s.Policy.TaskTypeNames(), req.TaskType)) {
		writeError(w, http.StatusBadRequest, "status must be succeeded|failed|cancelled (task_type must be known)")
		return
	}
	run, err := s.Ledger.FinishRun(r.Context(), r.PathValue("session_id"), req.Status, req.TaskType)
	if errors.Is(err, ledger.ErrNotFound) {
		writeError(w, http.StatusNotFound, "no run for session")
		return
	}
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, run)
}

func (s *Server) listApprovals(w http.ResponseWriter, r *http.Request) {
	if !s.ready() {
		writeError(w, http.StatusServiceUnavailable, "policy/ledger not configured")
		return
	}
	status := r.URL.Query().Get("status")
	if status != "" && !contains([]string{"pending", "approved", "rejected", "expired"}, status) {
		writeError(w, http.StatusBadRequest, "status must be pending|approved|rejected|expired")
		return
	}
	items, err := s.Ledger.ListApprovals(r.Context(), status)
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"approvals": nonNil(items)})
}

func (s *Server) resolveApproval(w http.ResponseWriter, r *http.Request) {
	if !s.ready() {
		writeError(w, http.StatusServiceUnavailable, "policy/ledger not configured")
		return
	}
	var req struct {
		Approve bool   `json:"approve"`
		By      string `json:"by"`
	}
	if !decodeBody(w, r, &req) {
		return
	}
	if req.By == "" {
		req.By = "user"
	}
	a, err := s.Ledger.ResolveApproval(r.Context(), r.PathValue("id"), req.Approve, req.By, s.Policy.Tier)
	if errors.Is(err, ledger.ErrNotFound) {
		writeError(w, http.StatusNotFound, "no pending approval with that id")
		return
	}
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, a)
}

func (s *Server) report(w http.ResponseWriter, r *http.Request) {
	if !s.ready() {
		writeError(w, http.StatusServiceUnavailable, "policy/ledger not configured")
		return
	}
	rows, err := s.Ledger.Report(r.Context(), r.PathValue("name"))
	if errors.Is(err, ledger.ErrNotFound) {
		names := make([]string, 0, len(ledger.Reports))
		for n := range ledger.Reports {
			names = append(names, n)
		}
		writeError(w, http.StatusNotFound, fmt.Sprintf("unknown report; available: %v", names))
		return
	}
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"report": r.PathValue("name"), "rows": nonNil(rows)})
}

func (s *Server) describePolicy(w http.ResponseWriter, _ *http.Request) {
	if s.Policy == nil {
		writeError(w, http.StatusServiceUnavailable, "policy not configured")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"default_model": s.Policy.DefaultModel, "ladder": s.Policy.Ladder,
		"models": s.Policy.Models, "task_types": s.Policy.TaskTypes, "agents": s.Policy.Agents})
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}

func contains(list []string, v string) bool {
	for _, x := range list {
		if x == v {
			return true
		}
	}
	return false
}

func nonNil[T any](s []T) []T {
	if s == nil {
		return []T{}
	}
	return s
}
