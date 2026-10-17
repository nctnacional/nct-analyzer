
"use strict";

document.addEventListener("DOMContentLoaded", () => {
    const $ = (id) => document.getElementById(id);

    let requestInProgress = false;
    let refreshTimer = null;
    let analysisEnabled = false;

    // =====================================================
    // UTILITARIOS
    // =====================================================

    function setText(id, value) {
        const element = $(id);
        if (!element) return;

        element.textContent =
            value === null || value === undefined || value === ""
                ? "—"
                : String(value);
    }

    function showMessage(id, message, type = "info") {
        const element = $(id);
        if (!element) return;

        element.textContent = message || "";
        element.hidden = !message;

        element.classList.remove(
            "error-message",
            "success-message",
            "warning-message",
            "feedback-message"
        );

        if (!message) return;

        const className = {
            error: "error-message",
            success: "success-message",
            warning: "warning-message"
        }[type] || "feedback-message";

        element.classList.add(className);
    }

    function setValue(id, value) {
        const element = $(id);
        if (!element || value === undefined || value === null) return;

        if (element.type === "checkbox") {
            element.checked = Boolean(value);
        } else {
            element.value = value;
        }
    }

    function formatDate(value) {
        if (!value) return "Ainda não realizada";

        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return String(value);

        return date.toLocaleString("pt-BR");
    }

    function formatNumber(value, digits = 2) {
        const number = Number(value);
        if (!Number.isFinite(number)) return String(value ?? "—");

        return number.toLocaleString("pt-BR", {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits
        });
    }

    function formatPercent(value) {
        const number = Number(value);
        if (!Number.isFinite(number)) return "—";

        return `${formatNumber(number > 0 && number <= 1 ? number * 100 : number)}%`;
    }

    function setLoading(button, loading, message = "Aguarde...") {
        if (!button) return;

        if (loading) {
            if (!button.dataset.originalText) {
                button.dataset.originalText = button.textContent;
            }

            button.disabled = true;
            button.textContent = message;
        } else {
            button.disabled = false;

            if (button.dataset.originalText) {
                button.textContent = button.dataset.originalText;
                delete button.dataset.originalText;
            }
        }
    }

    function csrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        if (meta) return meta.content;

        const input = document.querySelector('input[name="csrf_token"]');
        return input ? input.value : "";
    }

    // =====================================================
    // API HTTP
    // =====================================================

    async function api(url, options = {}) {
        const method = (options.method || "GET").toUpperCase();
        const headers = new Headers(options.headers || {});

        headers.set("Accept", "application/json");

        if (method !== "GET" && method !== "HEAD") {
            const token = csrfToken();

            if (token) {
                headers.set("X-CSRFToken", token);
            }

            if (options.body !== undefined) {
                headers.set("Content-Type", "application/json");
            }
        }

        const response = await fetch(url, {
            ...options,
            method,
            headers,
            credentials: "same-origin",
            cache: "no-store"
        });

        const contentType = response.headers.get("content-type") || "";
        let data;

        if (contentType.includes("application/json")) {
            data = await response.json();
        } else {
            const text = await response.text();
            data = { message: text };
        }

        if (!response.ok || data.success === false) {
            if (response.status === 401 || response.status === 403) {
                throw new Error(
                    "Acesso negado ou sessão expirada. Verifique seu login."
                );
            }

            throw new Error(
                data.error ||
                data.message ||
                `Erro HTTP ${response.status}`
            );
        }

        return data;
    }

    // =====================================================
    // CONFIGURACOES
    // =====================================================

    function getConfigFromForm() {
        return {
            timeframe: $("timeframe")?.value || "5min",

            window_candles: Number($("window_candles")?.value || 24),

            leaders: Number($("leaders")?.value || 4),

            followers_per_leader: Number(
                $("followers_per_leader")?.value || 6
            ),

            min_similarity: Number(
                $("min_similarity")?.value || 85
            ),

            update_seconds: Number(
                $("update_seconds")?.value || 180
            ),

            closed_candles_only:
                $("closed_candles_only")?.checked ?? true,

            automatic_pairs:
                $("automatic_pairs")?.checked ?? true
        };
    }

    function fillConfigForm(config = {}) {
        setValue("timeframe", config.timeframe ?? "5min");
        setValue("window_candles", config.window_candles ?? 24);
        setValue("leaders", config.leaders ?? 4);

        setValue(
            "followers_per_leader",
            config.followers_per_leader ?? 6
        );

        setValue("min_similarity", config.min_similarity ?? 85);
        setValue("update_seconds", config.update_seconds ?? 180);

        setValue(
            "closed_candles_only",
            config.closed_candles_only ?? true
        );

        setValue(
            "automatic_pairs",
            config.automatic_pairs ?? true
        );
    }

    async function loadConfig() {
        try {
            const response = await api("/api/config");
            const config = response.config || response.settings || response;

            fillConfigForm(config);
            updateKeyStatuses(config);

            updateMetrics(config, response);

            return config;
        } catch (error) {
            showMessage(
                "config-message",
                `Erro ao carregar configurações: ${error.message}`,
                "error"
            );

            return null;
        }
    }

    async function saveConfig(event) {
        if (event) event.preventDefault();

        const button = $("save-config");
        setLoading(button, true, "Salvando...");

        try {
            const response = await api("/api/config", {
                method: "POST",
                body: JSON.stringify(getConfigFromForm())
            });

            showMessage(
                "config-message",
                response.message || "Configurações salvas.",
                "success"
            );

            await loadConfig();
            await refreshState();
        } catch (error) {
            showMessage(
                "config-message",
                `Falha ao salvar: ${error.message}`,
                "error"
            );
        } finally {
            setLoading(button, false);
        }
    }

    // =====================================================
    // INDICADORES DO TOPO
    // =====================================================

    function updateMetrics(config = {}, state = {}) {
        setText("metric-leaders", config.leaders ?? "—");

        setText(
            "metric-followers",
            config.followers_per_leader ?? "—"
        );

        setText(
            "metric-similarity",
            config.min_similarity !== undefined
                ? `${config.min_similarity}%`
                : "—"
        );

        const count =
            config.api_keys_count ??
            state.api_keys_count ??
            getConfiguredKeysCount(config);

        setText("metric-keys", `${count}/4`);
    }

    function getConfiguredKeysCount(config = {}) {
        const keys = config.api_keys || config.keys || [];

        if (Array.isArray(keys)) {
            return keys.filter((item) => {
                if (typeof item === "boolean") return item;

                return Boolean(
                    item &&
                    (
                        item.configured === true ||
                        item.has_key === true ||
                        item.saved === true ||
                        item.status === "configured"
                    )
                );
            }).length;
        }

        if (keys && typeof keys === "object") {
            return Object.values(keys).filter((item) => {
                if (typeof item === "boolean") return item;

                return Boolean(
                    item &&
                    (
                        item.configured === true ||
                        item.has_key === true ||
                        item.saved === true
                    )
                );
            }).length;
        }

        return 0;
    }

    // =====================================================
    // CHAVES DE API
    // =====================================================

    function updateKeyStatuses(config = {}) {
        const keys = config.api_keys || config.keys || [];

        for (let number = 1; number <= 4; number++) {
            let entry = null;

            if (Array.isArray(keys)) {
                entry = keys.find((item) =>
                    Number(
                        item?.number ??
                        item?.key_number ??
                        item?.id
                    ) === number
                );

                if (!entry && keys[number - 1]) {
                    entry = keys[number - 1];
                }
            } else if (keys && typeof keys === "object") {
                entry =
                    keys[`api_key_${number}`] ??
                    keys[number] ??
                    null;
            }

            const configured = Boolean(
                entry === true ||
                (
                    entry &&
                    (
                        entry.configured === true ||
                        entry.has_key === true ||
                        entry.saved === true ||
                        entry.status === "configured" ||
                        entry.status === "ok"
                    )
                )
            );

            setText(
                `key-status-${number}`,
                configured ? "Chave cadastrada" : "Não configurada"
            );
        }

        updateMetrics(config);
    }

    async function saveKey(number) {
    const input = $(`api-key-${number}`);
    const button = document.querySelector(`[data-save-key="${number}"]`);
    const key = input ? input.value.trim() : "";

    if (!key) {
        showMessage("key-results", "Digite uma chave antes de salvar.", "warning");
        return;
    }

    setLoading(button, true, "Salvando...");

    try {
        const response = await api(`/api/keys/${number}`, {
            method: "POST",
            body: JSON.stringify({ key: key, api_key: key })
        });

        if (input) input.value = ""; // Limpa o campo de entrada por segurança

        showMessage(
            "key-results",
            response.message || `Chave ${number} salva com sucesso!`,
            "success"
        );

        await loadConfig();
        await refreshState();
    } catch (error) {
        showMessage(
            "key-results",
            `Erro ao salvar a chave ${number}: ${error.message || error}`,
            "error"
        );
    } finally {
        setLoading(button, false);
    }
}

    async function testKey(number) {
        const input = $(`api-key-${number}`);
        const button = document.querySelector(
            `[data-test-key="${number}"]`
        );

        const key = input?.value.trim() || "";

        setLoading(button, true, "Testando...");

        try {
            const options = { method: "POST" };

            if (key) {
                options.body = JSON.stringify({ api_key: key });
            }

            const response = await api(
                `/api/keys/test/${number}`,
                options
            );

            showMessage(
                "key-results",
                response.message ||
                    `Teste da chave ${number} concluído.`,
                response.success === false ? "warning" : "success"
            );

            if (response.message) {
                setText(`key-status-${number}`, response.message);
            }

            await loadConfig();
        } catch (error) {
            showMessage(
                "key-results",
                `Erro no teste da chave ${number}: ${error.message}`,
                "error"
            );
        } finally {
            setLoading(button, false);
        }
    }

    async function testAllKeys() {
        const button = $("test-all-keys");
        setLoading(button, true, "Testando...");

        try {
            const response = await api("/api/keys/test-all", {
                method: "POST",
                body: JSON.stringify({})
            });

            showMessage(
                "key-results",
                response.message || "Teste concluído.",
                response.success === false ? "warning" : "success"
            );

            await loadConfig();
        } catch (error) {
            showMessage(
                "key-results",
                `Erro ao testar as chaves: ${error.message}`,
                "error"
            );
        } finally {
            setLoading(button, false);
        }
    }

    // =====================================================
    // ESTADO DA ANALISE
    // =====================================================

    function setAnalysisStatus(enabled) {
        analysisEnabled = Boolean(enabled);

        setText(
            "system-analysis-status",
            analysisEnabled ? "Análise ativada" : "Análise desativada"
        );

        const startButton = $("start-analysis");
        const stopButton = $("stop-analysis");

        if (startButton) {
            startButton.disabled = analysisEnabled;
        }

        if (stopButton) {
            stopButton.disabled = !analysisEnabled;
        }
    }

    async function changeAnalysisState(desiredEnabled) {
        if (requestInProgress) return;

        if (Boolean(analysisEnabled) === Boolean(desiredEnabled)) {
            showMessage(
                "analysis-message",
                desiredEnabled
                    ? "A análise já está ativada."
                    : "A análise já está desativada.",
                "info"
            );
            return;
        }

        const button = desiredEnabled
            ? $("start-analysis")
            : $("stop-analysis");

        setLoading(
            button,
            true,
            desiredEnabled ? "Iniciando..." : "Parando..."
        );

        try {
            // A rota atual /api/toggle alterna o estado.
            const response = await api("/api/toggle", {
                method: "POST",
                body: JSON.stringify({ enabled: desiredEnabled })
            });

            showMessage(
                "analysis-message",
                response.message ||
                    "Solicitação enviada. Atualizando estado...",
                "success"
            );

            await refreshState();
        } catch (error) {
            showMessage(
                "analysis-message",
                `Não foi possível alterar o estado: ${error.message}`,
                "error"
            );
        } finally {
            setLoading(button, false);
            await refreshState();
        }
    }

    // =====================================================
    // RESULTADOS
    // =====================================================

    function parseResults(value) {
        if (!value) return {};

        if (typeof value === "string") {
            try {
                return JSON.parse(value);
            } catch {
                return {};
            }
        }

        return value;
    }

    function getLeaders(results) {
        results = parseResults(results);

        if (Array.isArray(results)) return results;
        if (Array.isArray(results.leaders)) return results.leaders;
        if (Array.isArray(results.results)) return results.results;
        if (Array.isArray(results.data)) return results.data;
        if (Array.isArray(results.items)) return results.items;

        return [];
    }

    function getFirst(object, names, fallback = null) {
        for (const name of names) {
            if (
                object &&
                object[name] !== undefined &&
                object[name] !== null &&
                object[name] !== ""
            ) {
                return object[name];
            }
        }

        return fallback;
    }

    function createElement(tag, className, text) {
        const element = document.createElement(tag);

        if (className) element.className = className;

        if (text !== null && text !== undefined) {
            element.textContent = String(text);
        }

        return element;
    }

    function renderResults(state) {
        // ID correto do dashboard atual.
        const container = $("results-area");
        if (!container) return;

        const results = parseResults(
            state.last_results_json ??
            state.last_results ??
            state.results ??
            null
        );

        const leaders = getLeaders(results);

        setText(
            "results-count",
            `${leaders.length} resultado${leaders.length === 1 ? "" : "s"}`
        );

        container.replaceChildren();

        if (leaders.length === 0) {
            const message = state.analysis_enabled
                ? "A análise está ativada, mas ainda não há resultados disponíveis."
                : "Nenhum resultado disponível. Configure as chaves e verifique o estado do sistema.";

            container.appendChild(
                createElement("div", "empty-results", message)
            );

            return;
        }

        const grid = createElement("div", "results-grid");

        leaders.forEach((leader, index) => {
            if (!leader || typeof leader !== "object") {
                leader = { symbol: String(leader) };
            }

            const symbol = getFirst(
                leader,
                ["symbol", "pair", "ticker", "name", "asset"],
                `Líder ${index + 1}`
            );

            const card = createElement("article", "result-card");
            const heading = createElement("h3", "", `${index + 1}. ${symbol}`);

            card.appendChild(heading);

            const score = getFirst(
                leader,
                ["score", "leader_score", "similarity", "similarity_pct"],
                null
            );

            if (score !== null) {
                const isSimilarity = (
                    "similarity" in leader ||
                    "similarity_pct" in leader
                );

                const value = isSimilarity
                    ? formatPercent(score)
                    : formatNumber(score);

                card.appendChild(
                    createElement("p", "", `Pontuação: ${value}`)
                );
            }

            const followers = getFirst(
                leader,
                [
                    "followers",
                    "related_markets",
                    "similar_markets",
                    "children"
                ],
                []
            );

            if (Array.isArray(followers)) {
                card.appendChild(
                    createElement(
                        "p",
                        "",
                        `Seguidores: ${followers.length}`
                    )
                );

                followers.forEach((follower) => {
                    const followerName =
                        typeof follower === "object" && follower !== null
                            ? getFirst(
                                follower,
                                ["symbol", "pair", "ticker", "name", "asset"],
                                "Ativo"
                            )
                            : follower;

                    card.appendChild(
                        createElement(
                            "p",
                            "",
                            `• ${followerName}`
                        )
                    );
                });
            }

            // Mostra outros campos recebidos sem executar HTML externo.
            const knownKeys = new Set([
                "symbol", "pair", "ticker", "name", "asset",
                "score", "leader_score", "similarity", "similarity_pct",
                "followers", "related_markets", "similar_markets", "children"
            ]);

            for (const [key, value] of Object.entries(leader)) {
                if (knownKeys.has(key)) continue;
                if (value === null || typeof value === "object") continue;

                card.appendChild(
                    createElement("p", "", `${key}: ${value}`)
                );
            }

            grid.appendChild(card);
        });

        container.appendChild(grid);
    }

    // =====================================================
    // ATUALIZACAO DO PAINEL
    // =====================================================

    async function refreshState() {
        if (requestInProgress) return;

        requestInProgress = true;

        try {
            const state = await api("/api/state");
            const config = state.config || state.settings || {};

            setAnalysisStatus(
                state.analysis_enabled ??
                state.enabled ??
                state.is_running ??
                false
            );

            const lastAnalysis =
                state.last_analysis_at ||
                state.last_update ||
                state.updated_at ||
                null;

            setText(
                "system-last-analysis",
                formatDate(lastAnalysis)
            );

            const error =
                state.last_analysis_error ||
                state.analysis_error ||
                "";

            setText(
                "system-last-error",
                error || "Nenhum erro registrado"
            );

            showMessage(
                "analysis-error",
                error,
                error ? "error" : "info"
            );

            updateMetrics(config, state);
            updateKeyStatuses(config);
            renderResults(state);

            return state;
        } catch (error) {
            showMessage(
                "analysis-error",
                `Falha ao atualizar o painel: ${error.message}`,
                "error"
            );

            setText(
                "system-analysis-status",
                "Sem comunicação com o servidor"
            );

            return null;
        } finally {
            requestInProgress = false;
        }
    }

    // =====================================================
    // EVENTOS
    // =====================================================

    $("config-form")?.addEventListener("submit", saveConfig);

    $("start-analysis")?.addEventListener("click", () => {
        changeAnalysisState(true);
    });

    $("stop-analysis")?.addEventListener("click", () => {
        changeAnalysisState(false);
    });

    $("refresh-state")?.addEventListener("click", async () => {
        const button = $("refresh-state");

        setLoading(button, true, "Atualizando...");

        await loadConfig();
        await refreshState();

        setLoading(button, false);
    });

    for (let number = 1; number <= 4; number++) {
        document
            .querySelector(`[data-save-key="${number}"]`)
            ?.addEventListener("click", () => saveKey(number));

        document
            .querySelector(`[data-test-key="${number}"]`)
            ?.addEventListener("click", () => testKey(number));
    }

    $("test-all-keys")?.addEventListener("click", testAllKeys);

    // =====================================================
    // INICIALIZACAO E ATUALIZACAO AUTOMATICA
    // =====================================================

    async function initializeDashboard() {
        await loadConfig();
        await refreshState();

        if (refreshTimer) {
            window.clearInterval(refreshTimer);
        }

        // Consulta o estado a cada 10 segundos.
        refreshTimer = window.setInterval(() => {
            refreshState();
        }, 10000);
    }

    initializeDashboard();
});


document.addEventListener("DOMContentLoaded", () => {

    // 1. Ouvinte para os botões de SALVAR CHAVE (ex: data-save-key="1")
    document.querySelectorAll("[data-save-key]").forEach(button => {
    button.addEventListener("click", async () => {
        const keyNum = button.getAttribute("data-save-key");
        const input = document.getElementById(`api-key-${keyNum}`);
        if (!input) return;

        const keyValue = input.value.trim();
        if (!keyValue) {
            alert("Digite uma chave antes de salvar.");
            return;
        }

        button.textContent = "Salvando...";
        button.disabled = true;

        try {
            // Obtém o token CSRF se houver no documento
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";

            const response = await fetch(`/api/keys/${keyNum}`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrfToken
                },
                body: JSON.stringify({ api_key: keyValue, key: keyValue })
            });

            const data = await response.json();

            if (response.ok && data.success) {
                alert(data.message || `Chave ${keyNum} salva com sucesso!`);
                location.reload();
            } else {
                alert(data.error || data.message || "Erro ao salvar a chave.");
                button.textContent = "Salvar";
                button.disabled = false;
            }
        } catch (err) {
            alert("Erro ao comunicar com o servidor: " + err.message);
            button.textContent = "Salvar";
            button.disabled = false;
        }
    });
});

    //2 Ouvinte para os botões INICIAR e PARAR
document.querySelectorAll("button").forEach(btn => {
    const text = btn.textContent.trim().toUpperCase();
    if (text === "INICIAR" || text === "START") {
        btn.addEventListener("click", () => toggleAnalysis(true));
    }
    if (text === "PARAR" || text === "STOP") {
        btn.addEventListener("click", () => toggleAnalysis(false));
    }
});

async function toggleAnalysis(enable) {
    try {
        const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";

        const response = await fetch("/api/toggle", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken
            },
            body: JSON.stringify({ enabled: enable })
        });

        const data = await response.json();

        if (response.ok && data.success) {
            alert(enable ? "Análise iniciada com sucesso!" : "Análise interrompida.");
            
            // Atualiza os estados visuais na tela imediatamente sem precisar de dar F5
            if (typeof refreshState === "function") refreshState();
            if (typeof loadConfig === "function") loadConfig();
            
           // location.reload();
           
        } else {
            alert(data.error || data.message || "Não foi possível alterar o estado da análise.");
        }
    } catch (err) {
        alert("Erro ao comunicar com o servidor: " + err.message);
    }
}

});