/**
 * Canonical field and schema registry (issue #86).
 *
 * Single source of truth for all data contracts:
 * - Screener column definitions (from index.html SCREENER_COLS)
 * - Tool manifest schema (issue #98)
 * - Research project schema (issue #98)
 *
 * This module is imported by all renderers/consumers so the contract
 * is defined once, not re-typed ad-hoc in multiple places.
 */

// ── Validation harness ────────────────────────────────────────────────

/**
 * Base validator. Returns {valid: bool, errors: string[]}.
 * Subclasses override validate() to add specific checks.
 */
class SchemaValidator {
  validate(obj) {
    throw new Error('validate() must be implemented by subclass');
  }
}

/**
 * Validate that an object matches a set of required/typed fields.
 * Used by ToolManifestValidator, ResearchProjectValidator.
 */
class ObjectValidator extends SchemaValidator {
  constructor(fields) {
    super();
    this.fields = fields; // {fieldName: {type, required, enum?, validator?}}
  }

  validate(obj) {
    const errors = [];
    if (typeof obj !== 'object' || obj === null) {
      return { valid: false, errors: ['Expected an object'] };
    }

    for (const [name, spec] of Object.entries(this.fields)) {
      const val = obj[name];
      const isPresent = val !== undefined && val !== null;

      if (spec.required && !isPresent) {
        errors.push(`Missing required field: ${name}`);
        continue;
      }

      if (isPresent) {
        // Type check
        if (spec.type && typeof val !== spec.type) {
          if (!(spec.type === 'array' && Array.isArray(val))) {
            errors.push(`Field ${name}: expected ${spec.type}, got ${typeof val}`);
            continue;
          }
        }

        // Enum check
        if (spec.enum && !spec.enum.includes(val)) {
          errors.push(`Field ${name}: value '${val}' not in [${spec.enum.join(', ')}]`);
        }

        // Custom validator
        if (spec.validator) {
          const subResult = spec.validator.validate(val);
          if (!subResult.valid) {
            errors.push(`Field ${name}: ${subResult.errors.join('; ')}`);
          }
        }
      }
    }

    return { valid: errors.length === 0, errors };
  }
}

// ── Tool Manifest Schema ───────────────────────────────────────────────

/**
 * Tool manifest entry schema (issue #98).
 *
 * Shared fields (all tools):
 *   - id: unique identifier (string)
 *   - name: human-readable name (string)
 *   - kind: 'gather' | 'model'
 *   - status: 'planned' | 'building' | 'working' | 'graduated' | 'retired'
 *   - purpose: one-line description (string)
 *   - doc: path to documentation (string)
 *   - lastRun: ISO 8601 timestamp or null
 *
 * Gather-only fields:
 *   - source: data source/upstream endpoint (string)
 *   - cadence: collection frequency (string, e.g. "nightly", "hourly")
 *   - outputPath: where collected data lives (string)
 *   - graduatedTo: null | Stock-Data-Pipeline location once moved upstream per #95 (string | null)
 *
 * Model-only fields:
 *   - inputs[]: array of field names this model consumes (string[])
 *   - outputs[]: array of field names this model produces (string[])
 *   - lookaheadPosture: 'none' | 'lag-prior-close' | 'lag-period' (forward bias is disclosed)
 */

const TOOL_MANIFEST_FIELDS = {
  id: { type: 'string', required: true },
  name: { type: 'string', required: true },
  kind: { type: 'string', required: true, enum: ['gather', 'model'] },
  status: { type: 'string', required: true, enum: ['planned', 'building', 'working', 'graduated', 'retired'] },
  purpose: { type: 'string', required: true },
  doc: { type: 'string', required: true },
  lastRun: { type: 'string', required: false }, // ISO 8601 or null

  // gather-only
  source: { type: 'string', required: false },
  cadence: { type: 'string', required: false },
  outputPath: { type: 'string', required: false },
  graduatedTo: { type: 'string', required: false },

  // model-only
  inputs: { type: 'array', required: false },
  outputs: { type: 'array', required: false },
  lookaheadPosture: { type: 'string', required: false, enum: ['none', 'lag-prior-close', 'lag-period'] },
};

class ToolManifestValidator extends ObjectValidator {
  constructor() {
    super(TOOL_MANIFEST_FIELDS);
  }

  validate(obj) {
    const { valid, errors } = super.validate(obj);
    if (!valid) return { valid, errors };

    const subErrors = [];

    // If kind==='gather', require gather-only fields; if 'model', require model-only fields
    if (obj.kind === 'gather') {
      if (!obj.source) subErrors.push('Gather tool must have source field');
      if (!obj.cadence) subErrors.push('Gather tool must have cadence field');
      if (!obj.outputPath) subErrors.push('Gather tool must have outputPath field');
    } else if (obj.kind === 'model') {
      if (!Array.isArray(obj.inputs) || obj.inputs.length === 0) {
        subErrors.push('Model tool must have non-empty inputs array');
      }
      if (!Array.isArray(obj.outputs) || obj.outputs.length === 0) {
        subErrors.push('Model tool must have non-empty outputs array');
      }
      if (!obj.lookaheadPosture) {
        subErrors.push('Model tool must have lookaheadPosture field');
      }
    }

    if (subErrors.length > 0) {
      return { valid: false, errors: [...errors, ...subErrors] };
    }

    return { valid: true, errors: [] };
  }
}

// ── Research Project Schema ────────────────────────────────────────────

/**
 * Research project entry schema (issue #98).
 *
 *   - id: unique identifier (string)
 *   - title: readable title (string)
 *   - hypothesis: null hypothesis being tested (string)
 *   - falsifier: condition that would falsify the hypothesis (string)
 *   - tools[]: array of tool manifest ids used by this project (string[])
 *   - trialCount: number of trials declared pre-run (number)
 *   - lookaheadPosture: disclosure of forward-bias posture (string)
 *   - status: 'planned' | 'running' | 'concluded' (string)
 *   - verdict: null | true | false (null = inconclusive, true/false = decisive) (boolean | null)
 *   - startedAt: ISO 8601 timestamp or null (string | null)
 *   - concludedAt: ISO 8601 timestamp or null (string | null)
 */

const RESEARCH_PROJECT_FIELDS = {
  id: { type: 'string', required: true },
  title: { type: 'string', required: true },
  hypothesis: { type: 'string', required: true },
  falsifier: { type: 'string', required: true },
  tools: { type: 'array', required: true },
  trialCount: { type: 'number', required: true },
  lookaheadPosture: { type: 'string', required: true, enum: ['none', 'lag-prior-close', 'lag-period'] },
  status: { type: 'string', required: true, enum: ['planned', 'running', 'concluded'] },
  verdict: { type: 'boolean', required: false }, // null is allowed
  startedAt: { type: 'string', required: false },
  concludedAt: { type: 'string', required: false },
};

class ResearchProjectValidator extends ObjectValidator {
  constructor(toolManifestRegistry) {
    super(RESEARCH_PROJECT_FIELDS);
    this.toolManifestRegistry = toolManifestRegistry;
  }

  validate(obj) {
    const { valid, errors } = super.validate(obj);
    if (!valid) return { valid, errors };

    const subErrors = [];

    // Validate that tools[] references exist in the manifest registry
    if (Array.isArray(obj.tools)) {
      for (const toolId of obj.tools) {
        if (!this.toolManifestRegistry.has(toolId)) {
          subErrors.push(`Tool reference '${toolId}' not found in tool manifest registry (dangling reference)`);
        }
      }
    }

    if (subErrors.length > 0) {
      return { valid: false, errors: [...errors, ...subErrors] };
    }

    return { valid: true, errors: [] };
  }
}

// ── Registry ───────────────────────────────────────────────────────────

/**
 * Central schema registry.
 * Holds tool manifest entries + research project entries.
 * Consumers import this and use it to access/validate both schemas.
 */
class SchemaRegistry {
  constructor() {
    this.tools = new Map(); // id -> tool manifest object
    this.projects = new Map(); // id -> research project object
    this.toolValidator = new ToolManifestValidator();
    this.projectValidator = new ResearchProjectValidator(this.tools);
  }

  /**
   * Add a tool manifest to the registry.
   * Returns {valid: bool, errors: string[], registered: bool}
   */
  addTool(tool) {
    const result = this.toolValidator.validate(tool);
    if (result.valid) {
      this.tools.set(tool.id, tool);
      result.registered = true;
    } else {
      result.registered = false;
    }
    return result;
  }

  /**
   * Add a research project to the registry.
   * Returns {valid: bool, errors: string[], registered: bool}
   */
  addProject(project) {
    const result = this.projectValidator.validate(project);
    if (result.valid) {
      this.projects.set(project.id, project);
      result.registered = true;
    } else {
      result.registered = false;
    }
    return result;
  }

  /**
   * Get a tool by id.
   */
  getTool(id) {
    return this.tools.get(id);
  }

  /**
   * Get a project by id.
   */
  getProject(id) {
    return this.projects.get(id);
  }

  /**
   * Get all tools.
   */
  getAllTools() {
    return Array.from(this.tools.values());
  }

  /**
   * Get all projects.
   */
  getAllProjects() {
    return Array.from(this.projects.values());
  }
}

// Export for use in renderers/consumers
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    SchemaRegistry,
    ToolManifestValidator,
    ResearchProjectValidator,
    ObjectValidator,
    SchemaValidator,
  };
}
