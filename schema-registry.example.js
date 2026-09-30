/**
 * Example usage of the canonical schema registry (issue #98).
 *
 * Renderers NEVER redefine tool-manifest or research-project schemas.
 * They import from schema-registry.js, validate against it, and use it.
 */

const { SchemaRegistry } = require('./schema-registry');

// Create the central registry (or initialize it as a global singleton)
const registry = new SchemaRegistry();

// ── Example: Tool manifest entries ──────────────────────────────────────

// Example gather tool: yfinance fundamentals collector
const fundamentalsTool = {
  id: 'fundamentals_collector',
  name: 'Fundamentals Collector',
  kind: 'gather',
  status: 'working',
  purpose: 'Daily collection of stock fundamentals (P/E, ROIC, EBIT/EV)',
  doc: 'Stock-Data-Pipeline/fundamentals.py',
  source: 'yfinance',
  cadence: 'nightly',
  outputPath: 'Stock-Data-Pipeline/data/fundamentals.json',
  graduatedTo: null,
  lastRun: '2026-08-03T22:15:00Z',
};

// Example model tool: Greenblatt magic formula
const magicFormulaTool = {
  id: 'magic_formula_ranker',
  name: 'Greenblatt Magic Formula',
  kind: 'model',
  status: 'working',
  purpose: 'Rank equities by Greenblatt ROC + EBIT/EV yield',
  doc: 'Education/summaries/greenblatt-magic-formula.md',
  inputs: ['roc_greenblatt', 'ebit_ev_yield'],
  outputs: ['magic_rank'],
  lookaheadPosture: 'none',
  lastRun: '2026-08-03T14:30:00Z',
};

// Add tools to registry
let result = registry.addTool(fundamentalsTool);
console.log(`Added fundamentalsTool:`, result);
result = registry.addTool(magicFormulaTool);
console.log(`Added magicFormulaTool:`, result);

// ── Example: Research project entry ────────────────────────────────────

const researchProject = {
  id: 'momentum_edge_2026',
  title: 'Fama-French momentum edge (2026 live)',
  hypothesis: 'Skip-month momentum (t-12 → t-2 return) predicts outperformance',
  falsifier: 'Walk-forward CSCV Sharpe drops below 0.2 (deflated)',
  tools: ['fundamentals_collector', 'magic_formula_ranker'],
  trialCount: 50, // 50 parameter combinations tried
  lookaheadPosture: 'lag-prior-close',
  status: 'running',
  verdict: null, // inconclusive until walk-forward completes
  startedAt: '2026-01-15T09:00:00Z',
  concludedAt: null,
};

result = registry.addProject(researchProject);
console.log(`Added research project:`, result);

// ── Example: Invalid entries (dangling tool reference) ───────────────────

const invalidProject = {
  id: 'bad_project',
  title: 'Project with dangling tool reference',
  hypothesis: 'This will fail validation',
  falsifier: 'Tool reference does not exist',
  tools: ['nonexistent_tool_id'], // ERROR: not in registry
  trialCount: 10,
  lookaheadPosture: 'none',
  status: 'planned',
  verdict: null,
  startedAt: null,
  concludedAt: null,
};

result = registry.addProject(invalidProject);
console.log(`Invalid project result:`, result);
// Output will show: valid: false, errors: ['Tool reference 'nonexistent_tool_id' not found...']

// ── Example: Renderer usage ─────────────────────────────────────────────

/**
 * Example: tool manifest renderer.
 * In a real React/Vue/web component, this would display a tool's metadata.
 */
function renderToolManifest(toolId) {
  const tool = registry.getTool(toolId);
  if (!tool) return `<p>Tool not found: ${toolId}</p>`;

  const isGather = tool.kind === 'gather';
  return `
    <div class="tool-manifest">
      <h3>${tool.name}</h3>
      <p><strong>Purpose:</strong> ${tool.purpose}</p>
      <p><strong>Status:</strong> <span class="status-${tool.status}">${tool.status}</span></p>
      <p><strong>Documentation:</strong> <a href="${tool.doc}">${tool.doc}</a></p>
      ${isGather ? `
        <p><strong>Source:</strong> ${tool.source}</p>
        <p><strong>Cadence:</strong> ${tool.cadence}</p>
        <p><strong>Output:</strong> ${tool.outputPath}</p>
        ${tool.graduatedTo ? `<p><strong>Graduated to:</strong> ${tool.graduatedTo}</p>` : ''}
      ` : `
        <p><strong>Inputs:</strong> ${tool.inputs.join(', ')}</p>
        <p><strong>Outputs:</strong> ${tool.outputs.join(', ')}</p>
        <p><strong>Look-ahead posture:</strong> ${tool.lookaheadPosture}</p>
      `}
      ${tool.lastRun ? `<p><strong>Last run:</strong> ${new Date(tool.lastRun).toLocaleString()}</p>` : ''}
    </div>
  `;
}

/**
 * Example: research project renderer.
 */
function renderResearchProject(projectId) {
  const project = registry.getProject(projectId);
  if (!project) return `<p>Project not found: ${projectId}</p>`;

  const toolNames = project.tools.map(id => registry.getTool(id)?.name || id).join(', ');
  return `
    <div class="research-project">
      <h3>${project.title}</h3>
      <p><strong>Hypothesis:</strong> ${project.hypothesis}</p>
      <p><strong>Falsifier:</strong> ${project.falsifier}</p>
      <p><strong>Status:</strong> <span class="status-${project.status}">${project.status}</span></p>
      <p><strong>Trials declared:</strong> ${project.trialCount}</p>
      <p><strong>Tools used:</strong> ${toolNames}</p>
      <p><strong>Look-ahead posture:</strong> ${project.lookaheadPosture}</p>
      ${project.verdict !== null ? `<p><strong>Verdict:</strong> ${project.verdict ? 'CONFIRMED' : 'REFUTED'}</p>` : ''}
      ${project.startedAt ? `<p><strong>Started:</strong> ${new Date(project.startedAt).toLocaleString()}</p>` : ''}
      ${project.concludedAt ? `<p><strong>Concluded:</strong> ${new Date(project.concludedAt).toLocaleString()}</p>` : ''}
    </div>
  `;
}

// Usage in a renderer:
console.log(renderToolManifest('magic_formula_ranker'));
console.log(renderResearchProject('momentum_edge_2026'));

// ── Key principles (issue #98 acceptance) ──────────────────────────────

/*
 * 1. Both schemas live in the registry (schema-registry.js)
 * 2. Renderers NEVER redeclare these schemas — they import & use
 * 3. Validator enforces dangling-reference check: a tools[] id with no
 *    matching manifest entry is a hard error (prevents drift)
 * 4. Follows the validation harness from #87 (ObjectValidator/SchemaValidator)
 *    rather than inventing a parallel validation layer
 */
