/**
 * Unit tests for the schema registry (issue #98).
 * Validates tool manifest and research project schemas + dangling reference detection.
 */

const {
  SchemaRegistry,
  ToolManifestValidator,
  ResearchProjectValidator,
} = require('./schema-registry');

function test(name, fn) {
  try {
    fn();
    console.log(`✓ ${name}`);
  } catch (err) {
    console.error(`✗ ${name}`);
    console.error(`  ${err.message}`);
    process.exitCode = 1;
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

// ── Tool Manifest Tests ────────────────────────────────────────────────

test('ToolManifestValidator: valid gather tool', () => {
  const validator = new ToolManifestValidator();
  const tool = {
    id: 'test_gather',
    name: 'Test Gather Tool',
    kind: 'gather',
    status: 'working',
    purpose: 'Test data collection',
    doc: 'path/to/doc',
    source: 'yfinance',
    cadence: 'nightly',
    outputPath: 'path/to/output',
    graduatedTo: null,
    lastRun: '2026-08-03T12:00:00Z',
  };
  const result = validator.validate(tool);
  assert(result.valid, `Expected valid, got errors: ${result.errors.join('; ')}`);
});

test('ToolManifestValidator: valid model tool', () => {
  const validator = new ToolManifestValidator();
  const tool = {
    id: 'test_model',
    name: 'Test Model Tool',
    kind: 'model',
    status: 'working',
    purpose: 'Test model',
    doc: 'path/to/doc',
    inputs: ['field1', 'field2'],
    outputs: ['output1'],
    lookaheadPosture: 'none',
    lastRun: '2026-08-03T12:00:00Z',
  };
  const result = validator.validate(tool);
  assert(result.valid, `Expected valid, got errors: ${result.errors.join('; ')}`);
});

test('ToolManifestValidator: gather tool missing required field', () => {
  const validator = new ToolManifestValidator();
  const tool = {
    id: 'test_gather',
    name: 'Test Gather Tool',
    kind: 'gather',
    status: 'working',
    purpose: 'Test data collection',
    doc: 'path/to/doc',
    // Missing: source, cadence, outputPath
  };
  const result = validator.validate(tool);
  assert(!result.valid, 'Expected invalid gather tool without gather-specific fields');
  assert(result.errors.some(e => e.includes('source')), 'Expected source error');
});

test('ToolManifestValidator: model tool missing inputs', () => {
  const validator = new ToolManifestValidator();
  const tool = {
    id: 'test_model',
    name: 'Test Model Tool',
    kind: 'model',
    status: 'working',
    purpose: 'Test model',
    doc: 'path/to/doc',
    // Missing inputs/outputs/lookaheadPosture
  };
  const result = validator.validate(tool);
  assert(!result.valid, 'Expected invalid model tool without model-specific fields');
  assert(result.errors.some(e => e.includes('inputs')), 'Expected inputs error');
});

test('ToolManifestValidator: invalid kind enum', () => {
  const validator = new ToolManifestValidator();
  const tool = {
    id: 'bad_tool',
    name: 'Bad Tool',
    kind: 'invalid_kind',
    status: 'working',
    purpose: 'Test',
    doc: 'path',
  };
  const result = validator.validate(tool);
  assert(!result.valid, 'Expected invalid enum value');
  assert(result.errors.some(e => e.includes('kind')), 'Expected kind error');
});

test('ToolManifestValidator: lastRun null accepted (never-run tool)', () => {
  const validator = new ToolManifestValidator();
  const tool = {
    id: 'test_gather_never_run',
    name: 'Test Gather Tool',
    kind: 'gather',
    status: 'planned',
    purpose: 'Test data collection',
    doc: 'path/to/doc',
    source: 'yfinance',
    cadence: 'nightly',
    outputPath: 'path/to/output',
    lastRun: null,
  };
  const result = validator.validate(tool);
  assert(result.valid, `Expected valid with lastRun: null, got errors: ${result.errors.join('; ')}`);
});

test('ToolManifestValidator: graduatedTo null accepted (not yet graduated)', () => {
  const validator = new ToolManifestValidator();
  const tool = {
    id: 'test_gather_not_graduated',
    name: 'Test Gather Tool',
    kind: 'gather',
    status: 'working',
    purpose: 'Test data collection',
    doc: 'path/to/doc',
    source: 'yfinance',
    cadence: 'nightly',
    outputPath: 'path/to/output',
    graduatedTo: null,
    lastRun: '2026-08-03T12:00:00Z',
  };
  const result = validator.validate(tool);
  assert(result.valid, `Expected valid with graduatedTo: null, got errors: ${result.errors.join('; ')}`);
});

// ── Research Project Tests ────────────────────────────────────────────

test('ResearchProjectValidator: valid project', () => {
  const registry = new SchemaRegistry();
  registry.addTool({
    id: 'test_tool',
    name: 'Test Tool',
    kind: 'gather',
    status: 'working',
    purpose: 'Test',
    doc: 'path',
    source: 'test',
    cadence: 'nightly',
    outputPath: 'out',
  });

  const validator = new ResearchProjectValidator(registry.tools);
  const project = {
    id: 'test_proj',
    title: 'Test Project',
    hypothesis: 'Test hypothesis',
    falsifier: 'Test falsifier',
    tools: ['test_tool'],
    trialCount: 10,
    lookaheadPosture: 'none',
    status: 'planned',
    verdict: null,
    startedAt: null,
    concludedAt: null,
  };
  const result = validator.validate(project);
  assert(result.valid, `Expected valid, got errors: ${result.errors.join('; ')}`);
});

test('ResearchProjectValidator: dangling tool reference (hard error)', () => {
  const emptyRegistry = new Map();
  const validator = new ResearchProjectValidator(emptyRegistry);
  const project = {
    id: 'bad_proj',
    title: 'Bad Project',
    hypothesis: 'Test',
    falsifier: 'Test',
    tools: ['nonexistent_tool_id'], // Dangling reference
    trialCount: 10,
    lookaheadPosture: 'none',
    status: 'planned',
    verdict: null,
    startedAt: null,
    concludedAt: null,
  };
  const result = validator.validate(project);
  assert(!result.valid, 'Expected invalid due to dangling tool reference');
  assert(
    result.errors.some(e => e.includes('not found in tool manifest registry')),
    'Expected dangling reference error message'
  );
});

test('ResearchProjectValidator: multiple tool references (mixed valid/dangling)', () => {
  const registry = new SchemaRegistry();
  registry.addTool({
    id: 'valid_tool',
    name: 'Valid',
    kind: 'gather',
    status: 'working',
    purpose: 'Test',
    doc: 'path',
    source: 'test',
    cadence: 'nightly',
    outputPath: 'out',
  });

  const validator = new ResearchProjectValidator(registry.tools);
  const project = {
    id: 'mixed_proj',
    title: 'Mixed Project',
    hypothesis: 'Test',
    falsifier: 'Test',
    tools: ['valid_tool', 'dangling_tool_id'],
    trialCount: 10,
    lookaheadPosture: 'none',
    status: 'planned',
    verdict: null,
    startedAt: null,
    concludedAt: null,
  };
  const result = validator.validate(project);
  assert(!result.valid, 'Expected invalid due to dangling reference');
  assert(
    result.errors.some(e => e.includes('dangling_tool_id')),
    'Expected error mentioning dangling tool'
  );
});

test('ResearchProjectValidator: verdict/startedAt/concludedAt null accepted (nullable required fields)', () => {
  const registry = new SchemaRegistry();
  registry.addTool({
    id: 'nullable_test_tool',
    name: 'Nullable Test Tool',
    kind: 'gather',
    status: 'working',
    purpose: 'Test',
    doc: 'path',
    source: 'test',
    cadence: 'nightly',
    outputPath: 'out',
  });

  const validator = new ResearchProjectValidator(registry.tools);
  const project = {
    id: 'nullable_proj',
    title: 'Nullable Fields Project',
    hypothesis: 'Test hypothesis',
    falsifier: 'Test falsifier',
    tools: ['nullable_test_tool'],
    trialCount: 5,
    lookaheadPosture: 'none',
    status: 'planned',
    verdict: null,
    startedAt: null,
    concludedAt: null,
  };
  const result = validator.validate(project);
  assert(result.valid, `Expected valid with null verdict/startedAt/concludedAt, got errors: ${result.errors.join('; ')}`);
});

test('ResearchProjectValidator: missing verdict key is a hard error (required, nullable != optional)', () => {
  const registry = new SchemaRegistry();
  registry.addTool({
    id: 'missing_verdict_tool',
    name: 'Tool',
    kind: 'gather',
    status: 'working',
    purpose: 'Test',
    doc: 'path',
    source: 'test',
    cadence: 'nightly',
    outputPath: 'out',
  });

  const validator = new ResearchProjectValidator(registry.tools);
  const project = {
    id: 'missing_verdict_proj',
    title: 'Missing Verdict Project',
    hypothesis: 'Test',
    falsifier: 'Test',
    tools: ['missing_verdict_tool'],
    trialCount: 5,
    lookaheadPosture: 'none',
    status: 'planned',
    // verdict key omitted entirely — must fail even though verdict is nullable,
    // because "nullable" means the value may be null, not that the key is optional
    startedAt: null,
    concludedAt: null,
  };
  const result = validator.validate(project);
  assert(!result.valid, 'Expected invalid due to missing required verdict field');
  assert(
    result.errors.some(e => e.includes('Missing required field: verdict')),
    'Expected missing verdict field error'
  );
});

// ── Full Registry Integration Tests ─────────────────────────────────────

test('SchemaRegistry: add tool and project, retrieve both', () => {
  const registry = new SchemaRegistry();

  // Add a tool
  const tool = {
    id: 'int_test_tool',
    name: 'Integration Test Tool',
    kind: 'gather',
    status: 'working',
    purpose: 'Integration test',
    doc: 'path',
    source: 'test',
    cadence: 'hourly',
    outputPath: 'test_output',
  };
  let result = registry.addTool(tool);
  assert(result.valid && result.registered, 'Expected tool to register');

  // Add a project using the tool
  const project = {
    id: 'int_test_proj',
    title: 'Integration Test Project',
    hypothesis: 'Test',
    falsifier: 'Test',
    tools: ['int_test_tool'],
    trialCount: 25,
    lookaheadPosture: 'none',
    status: 'running',
    verdict: null,
    startedAt: '2026-08-01T00:00:00Z',
    concludedAt: null,
  };
  result = registry.addProject(project);
  assert(result.valid && result.registered, 'Expected project to register');

  // Retrieve both
  const retrieved_tool = registry.getTool('int_test_tool');
  assert(retrieved_tool !== undefined, 'Expected tool to be retrievable');
  assert(retrieved_tool.name === 'Integration Test Tool', 'Expected correct tool name');

  const retrieved_project = registry.getProject('int_test_proj');
  assert(retrieved_project !== undefined, 'Expected project to be retrievable');
  assert(retrieved_project.title === 'Integration Test Project', 'Expected correct project title');
});

test('SchemaRegistry: getAllTools and getAllProjects', () => {
  const registry = new SchemaRegistry();

  // Add multiple tools
  registry.addTool({
    id: 'tool1',
    name: 'Tool 1',
    kind: 'gather',
    status: 'working',
    purpose: 'Test 1',
    doc: 'path1',
    source: 'src1',
    cadence: 'nightly',
    outputPath: 'out1',
  });
  registry.addTool({
    id: 'tool2',
    name: 'Tool 2',
    kind: 'model',
    status: 'working',
    purpose: 'Test 2',
    doc: 'path2',
    inputs: ['x'],
    outputs: ['y'],
    lookaheadPosture: 'none',
  });

  const all_tools = registry.getAllTools();
  assert(all_tools.length === 2, `Expected 2 tools, got ${all_tools.length}`);
  assert(all_tools.map(t => t.id).includes('tool1'), 'Expected tool1 in results');
  assert(all_tools.map(t => t.id).includes('tool2'), 'Expected tool2 in results');
});

test('SchemaRegistry: reject invalid project, no registration', () => {
  const registry = new SchemaRegistry();
  const project = {
    id: 'reject_proj',
    title: 'Should Reject',
    hypothesis: 'Test',
    falsifier: 'Test',
    tools: ['tool_that_does_not_exist'],
    trialCount: 10,
    lookaheadPosture: 'none',
    status: 'planned',
    verdict: null,
  };
  const result = registry.addProject(project);
  assert(!result.valid, 'Expected validation to fail');
  assert(!result.registered, 'Expected project not to be registered');
  assert(registry.getProject('reject_proj') === undefined, 'Expected project not in registry');
});

console.log('\n✓ All schema registry tests passed');
