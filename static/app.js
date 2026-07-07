/* DeepResearch — Frontend Logic */

const msgContainer = document.getElementById('messages');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('sendBtn');
const agentBar = document.getElementById('agentBar');
const agentSteps = document.getElementById('agentSteps');
const welcome = document.getElementById('welcome');
let loading = false;

// ── 会话级 threadId（刷新页面才重置）──────────────
const THREAD_ID = 'web-' + Date.now();

// ── Agent phases ─────────────────────────────────────

const AGENT_PHASES = ['intent', 'plan', 'web_scout', 'analyst', 'writer'];
const completedPhases = new Set();

function resetAgentBar() {
    completedPhases.clear();
    if (!agentBar) return;
    agentBar.style.display = 'none';
    if (agentSteps) {
        agentSteps.querySelectorAll('.step').forEach(s => { s.classList.remove('active', 'done'); });
    }
}

function setPhaseStatus(node, status) {
    if (!agentBar || !agentSteps) return;  // agent bar removed, no-op
    agentBar.style.display = 'block';
    const steps = agentSteps.querySelectorAll('.step');

    if (node === 'direct_answer') {
        completedPhases.add('intent');
        steps.forEach(s => s.classList.add('done'));
        return;
    }

    const idx = AGENT_PHASES.indexOf(node);
    if (idx < 0) return;

    if (status === 'start') {
        for (let i = 0; i < idx; i++) completedPhases.add(AGENT_PHASES[i]);
        steps.forEach(step => {
            const n = step.dataset.node;
            step.classList.remove('active', 'done');
            if (completedPhases.has(n)) step.classList.add('done');
            if (n === node) step.classList.add('active');
        });
    } else {
        completedPhases.add(node);
        steps.forEach(step => {
            const n = step.dataset.node;
            step.classList.remove('active');
            if (completedPhases.has(n)) step.classList.add('done');
        });
    }
}

// ── Workflow log entries ─────────────────────────────

function addWorkflowEntry(container, evt) {
    const entry = document.createElement('div');
    const status = evt.status || 'done';
    entry.className = 'wf-entry wf-' + status;
    const icon = evt.icon || '';
    const msg = evt.message || '';
    const detail = evt.detail || '';

    if (status === 'start') {
        entry.innerHTML = `<span class="wf-icon">${icon}</span> ` +
                          `<span class="wf-msg">${escapeHtml(msg)}</span>` +
                          `<span class="wf-spin"></span>`;
    } else {
        entry.innerHTML = `<span class="wf-icon">${icon}</span> ` +
                          `<span class="wf-msg">${escapeHtml(msg)}</span>` +
                          (detail ? `<span class="wf-detail">${escapeHtml(detail)}</span>` : '');
    }

    // 如果已有同节点条目，替换之
    const existing = container.querySelector(`[data-node="${evt.node}"]`);
    if (existing) existing.replaceWith(entry);
    entry.dataset.node = evt.node;
    container.appendChild(entry);
    scrollToBottom();
}

// ── Keyboard ─────────────────────────────────────────

function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
}

// ── Starter card click ───────────────────────────────

document.addEventListener('click', function (e) {
    const card = e.target.closest('.starter-card');
    if (card && !loading) {
        inputEl.value = card.dataset.query;
        sendMessage();
    }
});

// ── Send Message ─────────────────────────────────────

function sendMessage() {
    if (loading) return;
    const query = inputEl.value.trim();
    if (!query) return;
    inputEl.value = '';
    inputEl.style.height = 'auto';
    loading = true;
    sendBtn.disabled = true;
    resetAgentBar();

    if (welcome) welcome.style.display = 'none';

    // 用户消息
    appendMessage('user', query, false);

    // AI 容器
    const aiBubble = appendMessage('assistant', '', false);
    const aiContent = aiBubble.querySelector('.msg-content');
    aiContent.innerHTML = '';

    // 工作流日志区
    const workflowLog = document.createElement('div');
    workflowLog.className = 'workflow-log';
    aiBubble.insertBefore(workflowLog, aiContent);

    // Loading 指示器
    const loadingEl = document.createElement('div');
    loadingEl.className = 'loading-dots';
    loadingEl.innerHTML = '<span></span><span></span><span></span>';
    aiContent.appendChild(loadingEl);

    // SSE 连接
    fetch('/api/v1/research/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, user_id: 'web-user', thread_id: THREAD_ID }),
    }).then(async resp => {
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let finalContent = '';
        let sourceIndex = [];
        let evidence = [];

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                try {
                    const evt = JSON.parse(line.slice(6));
                    if (evt.type === 'phase') {
                        setPhaseStatus(evt.node, evt.status || 'done');
                        addWorkflowEntry(workflowLog, evt);
                    } else if (evt.type === 'reloop') {
                        // 分析师触发补充搜索循环
                        addWorkflowEntry(workflowLog, evt);
                        // 重置 web_scout 和 analyst 状态，准备新一轮
                        completedPhases.delete('web_scout');
                        completedPhases.delete('analyst');
                        if (agentSteps) {
                            agentSteps.querySelectorAll('.step').forEach(s => {
                                if (s.dataset.node === 'web_scout' || s.dataset.node === 'analyst') {
                                    s.classList.remove('done');
                                }
                            });
                        }
                    } else if (evt.type === 'final') {
                        finalContent = evt.final || '';
                        sourceIndex = evt.source_index || [];
                        evidence = evt.evidence || [];
                        const evidenceScores = evt.evidence_scores || [];
                        const quality = evt.quality || 'medium';
                        const qualityDetail = evt.quality_detail || '';

                        // 移除 loading
                        loadingEl.remove();

                        // 低质量警告条
                        if (quality === 'low') {
                            const warn = document.createElement('div');
                            warn.className = 'quality-warn';
                            warn.innerHTML = '⚠️ 本次研究未找到足够的高可信度来源（' + escapeHtml(qualityDetail) + '），以下结论仅供参考，建议核实关键数据。';
                            aiContent.appendChild(warn);
                        }

                        // 渲染报告
                        const reportDiv = document.createElement('div');
                        reportDiv.innerHTML = renderMarkdown(finalContent);
                        aiContent.appendChild(reportDiv);

                        // 标记全部完成
                        AGENT_PHASES.forEach(p => completedPhases.add(p));
                        if (agentSteps) {
                            agentSteps.querySelectorAll('.step').forEach(s => {
                                s.classList.remove('active');
                                s.classList.add('done');
                            });
                        }

                        // 追加参考链接块（带评分）
                        if (sourceIndex && sourceIndex.length > 0) {
                            appendReferences(aiContent, sourceIndex, evidenceScores);
                        }

                        // 工作流完成标记
                        const highCount = evidenceScores.filter(s => s.reliability >= 0.7).length;
                        const totalEvidence = evidenceScores.length || sourceIndex.length;
                        const qualityLabel = { high: '🟢', medium: '🟡', low: '🔴' }[quality] || '';
                        addWorkflowEntry(workflowLog, {
                            node: 'done',
                            icon: quality === 'low' ? '⚠️' : '✅',
                            message: quality === 'low' ? '研究完成（证据不足）' : '研究完成',
                            detail: `${qualityLabel} ${qualityDetail} · ${finalContent.length} 字`
                        });
                    } else if (evt.type === 'error') {
                        loadingEl.remove();
                        aiContent.innerHTML = '<p style="color:var(--red)">❌ 错误: ' + escapeHtml(evt.message) + '</p>';
                    }
                } catch (e) { /* skip malformed JSON */ }
            }
        }

        if (!finalContent) {
            loadingEl.remove();
            aiContent.innerHTML = '<p style="color:var(--text3)">（未获取到结果，请重试）</p>';
        }
    }).catch(err => {
        loadingEl.remove();
        aiContent.innerHTML = '<p style="color:var(--red)">❌ 请求失败: ' + escapeHtml(err.message) + '</p>';
    }).finally(() => {
        loading = false;
        sendBtn.disabled = false;
        inputEl.focus();
        scrollToBottom();
    });
}

// ── Append message ──────────────────────────────────

function appendMessage(role, content, isHtml) {
    const div = document.createElement('div');
    div.className = 'message ' + role;
    const inner = document.createElement('div');
    inner.className = 'msg-content';
    if (isHtml) {
        inner.innerHTML = content;
    } else {
        inner.textContent = content;
    }
    div.appendChild(inner);
    msgContainer.appendChild(div);
    scrollToBottom();
    return div;
}

// ── Reference links block ───────────────────────────

function appendReferences(container, sourceIndex, evidenceScores) {
    const refDiv = document.createElement('div');
    refDiv.className = 'ref-block';

    // 建立评分索引
    const scoreMap = {};
    if (evidenceScores && evidenceScores.length) {
        evidenceScores.forEach(s => { scoreMap[s.source_id] = s; });
    }

    // 按可信度从高到低排序
    const sorted = [...sourceIndex].sort((a, b) => {
        const sa = scoreMap[a.source_id || ''];
        const sb = scoreMap[b.source_id || ''];
        const ra = sa ? (sa.reliability || 0) : 0;
        const rb = sb ? (sb.reliability || 0) : 0;
        return rb - ra;
    });

    // 分组：高信度(≥0.7) 和 中信度(≥0.4)，低信度(<0.4) 不展示
    const highGroup = [];
    const midGroup = [];
    sorted.forEach(item => {
        const score = scoreMap[item.source_id || ''];
        const rel = score ? (score.reliability || 0) : 0;
        if (rel >= 0.7) highGroup.push({ item, rel, score });
        else if (rel >= 0.4) midGroup.push({ item, rel, score });
        // rel < 0.4: 不展示（后端已过滤，前端兜底隐藏）
    });

    const total = highGroup.length + midGroup.length;
    refDiv.innerHTML = `<div class="ref-title">📚 参考来源与证据质量（${total}条，按可信度排序）</div>`;

    function renderGroup(items) {
        items.forEach(({ item, rel, score }) => {
            const url = item.url || '';
            const label = (item.label || item.source_id || '').replace(/^\[.*?\]\s*/, '');
            const sid = item.source_id || '';
            const relPct = Math.round(rel * 100);
            let badge = '';
            if (rel >= 0.8) badge = `<span class="ql-badge ql-high" title="可信度 ${relPct}%">🟢 高 ${relPct}%</span>`;
            else if (rel >= 0.7) badge = `<span class="ql-badge ql-high" title="可信度 ${relPct}%">🟢 ${relPct}%</span>`;
            else if (rel >= 0.5) badge = `<span class="ql-badge ql-med" title="可信度 ${relPct}%">🟡 中 ${relPct}%</span>`;
            else badge = `<span class="ql-badge ql-med" title="可信度 ${relPct}%">🟡 ${relPct}%</span>`;

            if (url) {
                refDiv.innerHTML += `<div class="ref-row">${badge} <a href="${escapeAttr(url)}" target="_blank" rel="noopener">🔗 ${escapeHtml(label || url)}</a></div>`;
            } else {
                refDiv.innerHTML += `<div class="ref-row">${badge} <span style="color:var(--text3)">📄 ${escapeHtml(label)}</span></div>`;
            }
        });
    }

    if (highGroup.length) {
        refDiv.innerHTML += '<div class="ref-tier-label">🟢 高信度来源</div>';
        renderGroup(highGroup);
    }
    if (midGroup.length) {
        refDiv.innerHTML += '<div class="ref-tier-label">🟡 中信度来源</div>';
        renderGroup(midGroup);
    }

    container.appendChild(refDiv);
}

// ── Utils ────────────────────────────────────────────

function scrollToBottom() {
    const area = document.getElementById('chatArea');
    area.scrollTop = area.scrollHeight;
}

function escapeHtml(text) {
    const d = document.createElement('div');
    d.textContent = text;
    return d.innerHTML;
}

function escapeAttr(text) {
    return text.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Markdown renderer ────────────────────────────────

function renderMarkdown(text) {
    if (!text) return '';
    let html = text;

    // Fenced code blocks (must be before paragraphs)
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, function (_, lang, code) {
        return '<pre><code>' + escapeHtml(code.trim()) + '</code></pre>';
    });
    // Inline code
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

    // Headings
    html = html.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
    html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');

    // Bold + italic
    html = html.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');

    // Links
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

    // Tables: convert Markdown tables to HTML
    html = html.replace(/((?:^\|.+?\|$\n?)+)/gm, function (tableBlock) {
        const rows = tableBlock.trim().split('\n');
        if (rows.length < 2) return tableBlock;
        let tableHtml = '<table>';
        for (let i = 0; i < rows.length; i++) {
            const cells = rows[i].split('|').filter(c => c.trim() !== '');
            const isHeader = (i === 0) || (i === 1 && /^[\s\-:]+$/.test(rows[1].replace(/\|/g, '')));
            if (i === 1 && /^[\s\-:]+$/.test(rows[1].replace(/\|/g, ''))) continue; // skip separator row
            const tag = (i === 0) ? 'th' : 'td';
            tableHtml += '<tr>';
            cells.forEach(c => {
                tableHtml += `<${tag}>${c.trim()}</${tag}>`;
            });
            tableHtml += '</tr>';
        }
        tableHtml += '</table>';
        return tableHtml;
    });

    // HR
    html = html.replace(/^---$/gm, '<hr>');

    // Blockquote
    html = html.replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>');

    // Lists — use markers to distinguish ul from ol (fix $2 bug for numbered lists)
    html = html.replace(/^[\-\*] (.+)$/gm, '<!--ul--><li>$1</li>');
    html = html.replace(/^\d+\. (.+)$/gm, '<!--ol--><li>$1</li>');
    html = html.replace(/((?:<!--ul--><li>.*?<\/li>\s*)+)/g, '<ul>$1</ul>');
    html = html.replace(/((?:<!--ol--><li>.*?<\/li>\s*)+)/g, '<ol>$1</ol>');
    html = html.replace(/<!--ul-->/g, '');
    html = html.replace(/<!--ol-->/g, '');

    // Paragraphs
    const lines = html.split('\n');
    const result = [];
    let para = [];
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        const isBlock = /^<(h[1-4]|pre|ul|ol|li|blockquote|hr|div|table)/.test(line.trim());
        if (isBlock) {
            if (para.length) { result.push('<p>' + para.join('<br>') + '</p>'); para = []; }
            result.push(line);
        } else if (line.trim() === '') {
            if (para.length) { result.push('<p>' + para.join('<br>') + '</p>'); para = []; }
        } else {
            para.push(line);
        }
    }
    if (para.length) { result.push('<p>' + para.join('<br>') + '</p>'); }
    return result.join('\n');
}

// ── Citation linker ────────────────────────────────────

function linkCitations(html, sourceIndex) {
    if (!sourceIndex || !sourceIndex.length) return html;

    // Build map: citation number → {url, title}
    const numToSource = {};
    sourceIndex.forEach(s => {
        const sid = s.source_id || '';
        const m = sid.match(/(\d+)$/);
        if (m) {
            numToSource[m[1]] = { url: s.url || '', title: (s.label || s.source_id || '').replace(/^\[.*?\]\s*/, '') };
        }
    });

    // Replace <sup>[N]</sup> or <sup>[N,M,...]</sup> with clickable links
    return html.replace(/<sup>\[([^\]]+)\]<\/sup>/g, function (match, nums) {
        const parts = nums.split(',').map(function (n) { return n.trim(); });
        const linked = parts.map(function (n) {
            const src = numToSource[n];
            if (src && src.url) {
                return '<a href="' + escapeAttr(src.url) + '" target="_blank" rel="noopener" class="cite-link" title="' + escapeAttr(src.title) + '">[' + n + ']</a>';
            }
            return '[' + n + ']';
        }).join(',');
        return '<sup>' + linked + '</sup>';
    });
}

// ── Auto-resize textarea ─────────────────────────────

inputEl.addEventListener('input', function () {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});
