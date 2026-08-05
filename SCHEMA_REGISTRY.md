# Schema Registry (Issue #98)

Canonical definitions for **tool manifest** and **research project** schemas. Single source of truth — renderers import, never redeclare.

## Files

- **`schema-registry.js`** — Core registry, validators, and schemas
- **`schema-registry.example.js`** — Usage examples for renderers
- **`schema-registry.test.js`** — Unit tests (run with `node schema-registry.test.js`)
- **This file** — Documentation

## Schemas

### Tool Manifest

A tool manifest entry describes a data-collection tool or computational model.

```javascript
{
  // All tools
  id: string,                    // unique identifier
  name: string,                  // human-readable name
  kind: 'gather' | 'model',      // tool type
  status: 'planned'|'building'|'working'|'graduated'|'retired',
  purpose: string,               // one-line description
  doc: string,                   // path to documentation
  lastRun: string | null,        // ISO 8601 timestamp or null

  // Gather tools only
  source: string,                // data source/upstream endpoint
  cadence: string,               // collection frequency (e.g. 'nightly', 'hourly')
  outputPath: string,            // where collected data lives
  graduatedTo: string | null,    // null | Stock-Data-Pipeline location once moved upstream per #95

  // Model tools only
  inputs: string[],              // field names consumed by this model
  outputs: string[],             // field names produced by this model
  lookaheadPosture: 'none'|'lag-prior-close'|'lag-period'  // forward-bias disclosure
}
```

**Validation Rules:**
- `id`, `name`, `kind`, `status`, `purpose`, `doc` are required for all tools
- Gather tools must include: `source`, `cadence`, `outputPath`
- Model tools must include: `inputs` (non-empty), `outputs` (non-empty), `lookaheadPosture`
- `kind` must be exactly `'gather'` or `'model'`
- `status` must be one of the five enumerated values

### Research Project

A research project entry describes a hypothesis test or trading strategy validation.

```javascript
{
  id: string,                              // unique identifier
  title: string,                           // readable title
  hypothesis: string,                      // null hypothesis being tested
  falsifier: string,                       // condition that would refute the hypothesis
  tools: string[],                         // tool manifest ids used by this project
  trialCount: number,                      // trial count declared pre-run
  lookaheadPosture: 'none'|'lag-prior-close'|'lag-period', // forward-bias disclosure
  status: 'planned' | 'running' | 'concluded',
  verdict: boolean | null,                 // null = inconclusive; true/false = decisive
  startedAt: string | null,                // ISO 8601 timestamp or null
  concludedAt: string | null               // ISO 8601 timestamp or null
}
```

**Validation Rules:**
- All fields shown are required
- `tools[]` array entries must reference existing tool manifest entries by `id`
- **Dangling references are a hard error** — if a tool id in `tools[]` has no matching manifest entry, validation fails and the project is not registered (prevents drift point #1)
- `status` must be exactly one of the three enumerated values
- `verdict` can be `true`, `false`, or `null` (null = trial still running / inconclusive)

## Usage

### Consumers: Import and Use

```javascript
// In your renderer or data-processing code
const { SchemaRegistry } = require('./schema-registry');

const registry = new SchemaRegistry();

// Add a tool
const tool = { id: 'my_tool', name: '...', kind: 'gather', ... };
const result = registry.addTool(tool);
if (!result.valid) {
  console.error('Tool validation failed:', result.errors);
  return;
}

// Add a project (references the tool)
const project = { id: 'my_proj', ..., tools: ['my_tool'], ... };
const result = registry.addProject(project);
if (!result.valid) {
  console.error('Project validation failed:', result.errors);
  return;
}

// Render or process
const tool = registry.getTool('my_tool');
const project = registry.getProject('my_proj');
const allTools = registry.getAllTools();
const allProjects = registry.getAllProjects();
```

### Renderers: Never Redefine

❌ **Don't do this:**
```javascript
// BAD: Redefining the schema ad-hoc in a renderer
const TOOL_MANIFEST_SCHEMA = {
  id: 'string',
  name: 'string',
  // ... redeclared in multiple places, becoming drift points
};
```

✅ **Do this instead:**
```javascript
// GOOD: Import from the canonical registry
const { SchemaRegistry } = require('./schema-registry');
const registry = new SchemaRegistry();
registry.addTool(toolData);  // Validates against the canonical schema
```

## Validation Harness

The registry uses a composable validator hierarchy:

- **`SchemaValidator`** — Base class with `validate(obj)` returning `{valid, errors}`
- **`ObjectValidator`** — Generic field-level validation (type, required, nullable, enum, custom). `required` and `nullable` are independent: `required: true, nullable: true` means the key must be present but its value may be `null` (e.g. `verdict`, `startedAt`, `concludedAt`, `lastRun`).
- **`ToolManifestValidator`** — Extends `ObjectValidator`, enforces gather/model field rules
- **`ResearchProjectValidator`** — Extends `ObjectValidator`, enforces dangling-reference check

This follows the validation pattern established in #87 (canonical field-schema registry), rather than inventing a parallel system.

## Acceptance (Issue #98)

✓ Both tool-manifest and research-project schemas live in `schema-registry.js` (not scattered across renderers)  
✓ Validator extended so a `tools[]` id with no matching manifest entry is a hard error (dangling references detected)  
✓ Follows validation harness from #87 rather than inventing parallel validation  
✓ Renderers import, never redeclare

## Upcoming Work

- **#87** — Migrate screener column definitions (`SCREENER_COLS` from `index.html`) to the registry
- **#95** — Tool graduation boundary: `graduatedTo` field points to Stock-Data-Pipeline location once moved upstream
- Future renderers (tool manifest browser, research project dashboard) will import `SchemaRegistry` and use `getTool()` / `getProject()` / `getAllTools()` / `getAllProjects()` instead of maintaining separate schema definitions

## Testing

Run the full test suite:

```bash
node schema-registry.test.js
```

Tests cover:
- Valid gather and model tools
- Valid research projects
- Required field enforcement
- Enum value validation
- Dangling reference detection (hard error)
- Registry retrieval and iteration
