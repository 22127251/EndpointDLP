<template>
  <div class="page-container">
    <div class="header-section">
      <div>
        <h1 class="title">Security Policies</h1>
        <p class="subtitle">Manage DLP rules and deployment targets.</p>
      </div>
      <el-button type="primary" :icon="Plus" @click="handleOpenDialog()"
        >Create New Policy</el-button
      >
    </div>
    <div class="header-actions">
      <el-dropdown
        @command="(format) => exportData(policies, 'all_policies', format)"
      >
        <el-button :icon="Download">Export All</el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="json">Download JSON</el-dropdown-item>
            <el-dropdown-item command="yaml">Download YAML</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </div>

    <!-- TABLE -->

    <el-card shadow="never">
      <!-- SEARCH BAR -->
      <div class="toolbar">
        <el-input
          v-model="searchQuery"
          placeholder="Search by name"
          :prefix-icon="Search"
          clearable
          style="width: 350px"
          @input="handleSearch"
        />
        <el-button :icon="Refresh" @click="fetchData">Reload</el-button>
      </div>

      <!-- POLICY TABLE -->
      <el-table :data="policies" v-loading="loading">
        <el-table-column prop="name" label="POLICY NAME" min-width="220" />
        <el-table-column label="CHANNELS" min-width="180">
          <template #default="{ row }">
            <el-tag
              v-for="ch in (row.channels || [])"
              :key="ch"
              size="small"
              effect="plain"
              class="channel-tag"
            >{{ ch.toUpperCase() }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="TYPE" width="100">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.type?.toUpperCase() }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="ACTION" width="160">
          <template #default="{ row }">
            <el-tooltip :content="ladderSummary(row)" placement="top">
              <el-tag :type="getActionType(topAction(row))" effect="dark">
                {{ topAction(row).toUpperCase() }}
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="STATUS" width="100">
          <template #default="{ row }">
            <el-switch
              v-model="row.is_active"
              @change="handleToggleStatus(row)"
            />
          </template>
        </el-table-column>
        <el-table-column label="MANAGE" width="200" align="right">
          <template #default="{ row }">
            <el-dropdown
              @command="(format) => exportData(row, `policy_${row.id}`, format)"
            >
              <el-button link :icon="Document" />
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="json">JSON</el-dropdown-item>
                  <el-dropdown-item command="yaml">YAML</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>

            <el-button link :icon="Edit" @click="handleOpenDialog(row)"
              >Edit</el-button
            >
            <el-button link @click="handleOpenAssign(row)"
              >Assign</el-button
            >
            <el-button
              link
              type="danger"
              :icon="Delete"
              @click="handleDelete(row.id)"
            />
          </template>
        </el-table-column>
      </el-table>

      <!-- PAGINATION -->
      <div class="pagination-container">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          :total="total"
          @size-change="fetchData"
          @current-change="fetchData"
        />
      </div>
    </el-card>

    <!-- CREATE/EDIT DIALOG -->
    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? 'Edit Policy' : 'Create New Policy'"
      width="700px"
    >
      <el-form :model="form" label-position="top">
        <el-form-item label="Policy Name" required>
          <el-input v-model="form.name" />
        </el-form-item>

        <el-row :gutter="20">
          <el-col :span="12">
            <el-form-item label="Rule Type">
              <el-select v-model="form.type" style="width: 100%" @change="onTypeChange">
                <el-option
                  v-for="t in constants.rule_types"
                  :key="t"
                  :label="t.toUpperCase()"
                  :value="t"
                />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="Block Reason (shown to the user)">
              <el-input
                v-model="form.user_message"
                placeholder="e.g. Credit card number detected"
              />
            </el-form-item>
          </el-col>
        </el-row>

        <!-- CHANNELS (MULTI-SELECT) -->
        <el-form-item label="Channels">
          <el-select
            v-model="form.channels"
            multiple
            style="width: 100%"
            placeholder="Select channels"
          >
            <el-option
              v-for="c in constants.channels"
              :key="c"
              :label="c.toUpperCase()"
              :value="c"
            />
          </el-select>
        </el-form-item>

        <!-- RULE CONFIG (flexible per rule type) -->
        <div class="rule-section">
          <div class="section-label">Rule Config</div>

          <template v-for="field in activeRuleFields" :key="field.key">
            <el-form-item :label="field.label">
              <!-- Single-line list (patterns, keywords, etc.) -->
              <template v-if="field.widget === 'list'">
                <div v-for="(val, index) in form[field.key]" :key="index" class="rule-row">
                  <el-input
                    v-model="form[field.key][index]"
                    :placeholder="field.placeholder"
                  />
                  <el-button
                    type="danger"
                    :icon="Delete"
                    circle
                    size="small"
                    @click="form[field.key].splice(index, 1)"
                    :disabled="form[field.key].length <= 1"
                  />
                </div>
                <el-button
                  type="primary"
                  link
                  :icon="Plus"
                  @click="form[field.key].push('')"
                >
                  {{ field.addLabel || `Add ${field.label.slice(0, -1)}` }}
                </el-button>
                <div v-if="field.hint" class="field-hint field-hint-block">{{ field.hint }}</div>
              </template>

              <!-- Number input -->
              <template v-else-if="field.widget === 'number'">
                <el-input-number
                  v-model="form[field.key]"
                  :min="field.min ?? 0"
                  :max="field.max ?? 1000"
                  :step="field.step ?? 10"
                />
                <span class="field-hint">{{ field.hint }}</span>
              </template>
            </el-form-item>
          </template>
        </div>

        <!-- SCORING + ACTION LADDER (the sole action mechanism) -->
        <div class="rule-section">
          <div class="section-label">Detection Scoring &amp; Action Ladder</div>
          <el-row :gutter="20">
            <el-col :span="12">
              <el-form-item label="Base Score (format match)">
                <el-input-number v-model="form.score_base" :min="0" :max="2" :step="0.1" />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="Context Boost (nearby context word)">
                <el-input-number v-model="form.score_context_boost" :min="0" :max="2" :step="0.1" />
              </el-form-item>
            </el-col>
          </el-row>
          <p class="field-hint">
            No-context score = base; with-context = base + boost. Each rung maps
            a score → an action (evaluated high → low).
          </p>
          <el-form-item label="Action Ladder">
            <div v-for="(rung, index) in form.actions" :key="index" class="rule-row">
              <span class="ladder-ge">&ge;</span>
              <el-input-number
                v-model="rung.min_score"
                :min="0"
                :max="2"
                :step="0.1"
                style="width: 130px"
              />
              <span class="ladder-arrow">&rarr;</span>
              <el-select v-model="rung.action" style="width: 160px">
                <el-option
                  v-for="a in constants.actions"
                  :key="a"
                  :label="a.toUpperCase()"
                  :value="a"
                />
              </el-select>
              <el-button
                type="danger"
                :icon="Delete"
                circle
                size="small"
                @click="form.actions.splice(index, 1)"
                :disabled="form.actions.length <= 1"
              />
            </div>
            <el-button
              type="primary"
              link
              :icon="Plus"
              @click="form.actions.push({ min_score: 0, action: 'allow_log' })"
            >
              Add Rung
            </el-button>
          </el-form-item>
        </div>

        <el-form-item label="Description" class="mt-4">
          <el-input v-model="form.description" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">Cancel</el-button>
        <el-button type="primary" :loading="submitting" @click="handleSubmit"
          >Save Policy</el-button
        >
      </template>
    </el-dialog>

    <!-- ASSIGN DIALOG -->
    <el-dialog
      v-model="assignDialogVisible"
      :title="`Assign Policy: ${assignPolicy?.name || ''}`"
      width="600px"
    >
      <el-tabs v-model="assignTab">
        <el-tab-pane label="Agents" name="agents">
          <el-table :data="allAgents" v-loading="assignLoading" max-height="400">
            <el-table-column width="50">
              <template #default="{ row }">
                <el-checkbox
                  :model-value="assignAgentIds.includes(row.id)"
                  @change="(val) => toggleAssignAgent(row.id, val)"
                />
              </template>
            </el-table-column>
            <el-table-column prop="hostname" label="HOSTNAME" />
            <el-table-column prop="status" label="STATUS" width="100">
              <template #default="{ row }">
                <el-tag size="small" :type="row.status === 'active' ? 'success' : 'info'">
                  {{ row.status }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
        <el-tab-pane label="Groups" name="groups">
          <el-table :data="allGroups" v-loading="assignLoading" max-height="400">
            <el-table-column width="50">
              <template #default="{ row }">
                <el-checkbox
                  :model-value="assignGroupIds.includes(row.id)"
                  @change="(val) => toggleAssignGroup(row.id, val)"
                />
              </template>
            </el-table-column>
            <el-table-column prop="name" label="GROUP NAME" />
          </el-table>
        </el-tab-pane>
      </el-tabs>

      <template #footer>
        <el-button @click="assignDialogVisible = false">Cancel</el-button>
        <el-button type="primary" :loading="assignSubmitting" @click="handleAssignSubmit">
          Save Assignment
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from "vue";
import {
  Plus,
  Edit,
  Delete,
  Download,
  Document,
  Search,
  Refresh,
} from "@element-plus/icons-vue";
import apiClient from "@/api/axios";
import { ElMessage, ElMessageBox } from "element-plus";
import { exportData } from "@/utils/exporter";

const loading = ref(false);
const submitting = ref(false);
const dialogVisible = ref(false);
const isEdit = ref(false);
const policies = ref([]);
const page = ref(1);
const pageSize = ref(20);
const total = ref(0);
const searchQuery = ref("");

const handleExport = (format) => {
  const dataToExport = policies.value;
  exportData(dataToExport, "policies", format);
};

// Metadata constants from Backend
const constants = ref({ actions: [], channels: [], rule_types: [] });

// Canonical new-policy ladder: format + context -> block, format only -> allow_log.
const defaultActions = () => [
  { min_score: 1.0, action: "block" },
  { min_score: 0.0, action: "allow_log" },
];

const form = ref({
  id: null,
  name: "",
  description: "",
  type: "regex",
  patterns: [""],
  keywords: [""],
  channels: ["browser", "clipboard", "peripheral_storage"],
  user_message: "",
  score_base: 0.5,
  score_context_boost: 0.5,
  actions: defaultActions(),
  context_words: [],
  context_range: 0,
  is_active: true,
});

// ── Rule config: defines which fields each rule type shows ──
// Each field: { key, label, widget ("list" | "number"), placeholder, ... }
const RULE_FIELDS = {
  regex: [
    { key: "patterns", label: "Patterns", widget: "list", placeholder: "Regex pattern (e.g. \\b4[0-9]{3}...\\b)", addLabel: "Add Pattern", hint: "Enter the raw regex with SINGLE backslashes, e.g. \\b\\d{12}\\b. Do NOT double-escape: \\\\b stores a literal backslash and never matches." },
    { key: "context_words", label: "Context Words", widget: "list", placeholder: "Context word (e.g. credit card)", addLabel: "Add Context Word" },
    { key: "context_range", label: "Context Range (characters)", widget: "number", min: 0, max: 1000, step: 10, hint: "How many characters around the match to check for context words. 0 = disabled." },
  ],
  keyword: [
    { key: "keywords", label: "Keywords", widget: "list", placeholder: "Keyword (e.g. confidential)", addLabel: "Add Keyword" },
    { key: "context_words", label: "Context Words", widget: "list", placeholder: "Context word (e.g. credit card)", addLabel: "Add Context Word" },
    { key: "context_range", label: "Context Range (characters)", widget: "number", min: 0, max: 1000, step: 10, hint: "How many characters around the match to check for context words. 0 = disabled." },
  ],
  denylist: [
    { key: "keywords", label: "Keywords", widget: "list", placeholder: "Keyword (e.g. confidential)", addLabel: "Add Keyword" },
    { key: "context_words", label: "Context Words", widget: "list", placeholder: "Context word (e.g. credit card)", addLabel: "Add Context Word" },
    { key: "context_range", label: "Context Range (characters)", widget: "number", min: 0, max: 1000, step: 10, hint: "How many characters around the match to check for context words. 0 = disabled." },
  ],
};

// Computed: fields to show for current rule type
const activeRuleFields = computed(() => RULE_FIELDS[form.value.type] || []);

// When rule type changes, ensure the correct arrays exist on form
const onTypeChange = () => {
  const type = form.value.type;
  if (type === "regex") {
    if (!form.value.patterns || form.value.patterns.length === 0) form.value.patterns = [""];
    form.value.keywords = [];
  } else {
    if (!form.value.keywords || form.value.keywords.length === 0) form.value.keywords = [""];
    form.value.patterns = [];
  }
};

let searchTimer = null;

const fetchData = async () => {
  loading.value = true;
  try {
    const [pRes, mRes] = await Promise.all([
      apiClient.get("/policies/", {
        params: {
          page: page.value,
          page_size: pageSize.value,
          search: searchQuery.value,
        },
      }),
      apiClient.get("/metadata/constants"),
    ]);

    policies.value = pRes.data.items || pRes.data;
    total.value = pRes.data.total || 0;
    constants.value = {
      actions: mRes.data.policy_actions || [],
      channels: mRes.data.policy_channels || [],
      rule_types: mRes.data.rule_types || [],
    };
  } finally {
    loading.value = false;
  }
};

const handleSearch = () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    page.value = 1;
    fetchData();
  }, 500);
};

const handleOpenDialog = (row = null) => {
  if (row) {
    isEdit.value = true;
    form.value = {
      ...row,
      patterns: row.patterns?.length > 0 ? [...row.patterns] : [""],
      keywords: row.keywords?.length > 0 ? [...row.keywords] : [""],
      channels: row.channels?.length > 0 ? [...row.channels] : [],
      context_words: row.context_words?.length > 0 ? [...row.context_words] : [],
      context_range: row.context_range || 0,
      user_message: row.user_message || "",
      score_base: row.score_base ?? 0.5,
      score_context_boost: row.score_context_boost ?? 0.5,
      actions:
        row.actions?.length > 0
          ? row.actions.map((r) => ({ ...r }))
          : defaultActions(),
    };
  } else {
    isEdit.value = false;
    form.value = {
      name: "",
      description: "",
      type: "regex",
      patterns: [""],
      keywords: [""],
      channels: ["browser", "clipboard", "peripheral_storage"],
      user_message: "",
      score_base: 0.5,
      score_context_boost: 0.5,
      actions: defaultActions(),
      context_words: [],
      context_range: 0,
      is_active: true,
    };
  }
  dialogVisible.value = true;
};

const handleSubmit = async () => {
  submitting.value = true;

  // Clean up empty patterns/keywords
  const payload = { ...form.value };
  if (payload.type === "regex") {
    payload.patterns = payload.patterns.filter((p) => p.trim());
    payload.keywords = [];
  } else {
    payload.keywords = payload.keywords.filter((k) => k.trim());
    payload.patterns = [];
  }
  payload.context_words = payload.context_words.filter((c) => c.trim());

  // Action ladder: keep complete rungs, ordered high -> low by min_score.
  payload.actions = (payload.actions || [])
    .filter((r) => r.action != null && r.min_score != null)
    .map((r) => ({ min_score: Number(r.min_score), action: r.action }))
    .sort((a, b) => b.min_score - a.min_score);

  // Guard: a pattern with a double backslash is almost always a YAML-escaped form
  // copied by mistake (it stores a literal "\" and never matches). Warn, don't block.
  if (payload.type === "regex" && payload.patterns.some((p) => p.includes("\\\\"))) {
    try {
      await ElMessageBox.confirm(
        'A pattern contains a double backslash ("\\\\"). Enter the raw regex with single backslashes (e.g. \\b\\d{12}\\b); a double backslash usually means it was copied with YAML escaping and will NOT match. Save anyway?',
        "Possible double-escaped pattern",
        { confirmButtonText: "Save anyway", cancelButtonText: "Go back & fix", type: "warning" }
      );
    } catch {
      submitting.value = false;
      return; // user chose to fix it
    }
  }

  try {
    if (isEdit.value) {
      await apiClient.put(`/policies/${payload.id}`, payload);
      ElMessage.success("Policy updated");
    } else {
      await apiClient.post("/policies/", payload);
      ElMessage.success("Policy created");
    }
    dialogVisible.value = false;
    fetchData();
  } catch (e) {
    ElMessage.error("Error saving policy");
  } finally {
    submitting.value = false;
  }
};

const handleToggleStatus = async (row) => {
  try {
    await apiClient.put(`/policies/${row.id}`, { is_active: row.is_active });
    ElMessage.success(`Policy ${row.is_active ? "enabled" : "disabled"}`);
  } catch (e) {
    row.is_active = !row.is_active;
  }
};

const handleDelete = (id) => {
  ElMessageBox.confirm("Delete this policy permanently?").then(async () => {
    await apiClient.delete(`/policies/${id}`);
    fetchData();
  });
};

// ── Assign policy to agents / groups ──
const assignDialogVisible = ref(false);
const assignTab = ref("agents");
const assignPolicy = ref(null);
const assignLoading = ref(false);
const assignSubmitting = ref(false);
const allAgents = ref([]);
const allGroups = ref([]);
const assignAgentIds = ref([]);
const assignGroupIds = ref([]);

const handleOpenAssign = async (row) => {
  assignPolicy.value = row;
  assignTab.value = "agents";
  assignAgentIds.value = (row.policies ? [] : []); // will be populated from row if available
  assignGroupIds.value = [];
  assignDialogVisible.value = true;

  // Pre-select agents/groups already assigned
  assignLoading.value = true;
  try {
    const [aRes, gRes] = await Promise.all([
      apiClient.get("/agents/", { params: { page: 1, page_size: 100 } }),
      apiClient.get("/agent-groups/", { params: { page: 1, page_size: 100 } }),
    ]);
    allAgents.value = aRes.data.items || [];
    allGroups.value = gRes.data.items || [];

    // Load current assignments from the policy detail
    const pRes = await apiClient.get(`/policies/${row.id}`);
    const policyDetail = pRes.data;
    assignAgentIds.value = (policyDetail.individual_agents || []).map((a) => a.id);
    assignGroupIds.value = (policyDetail.agent_groups || []).map((g) => g.id);
  } finally {
    assignLoading.value = false;
  }
};

const toggleAssignAgent = (id, checked) => {
  if (checked) {
    if (!assignAgentIds.value.includes(id)) assignAgentIds.value.push(id);
  } else {
    assignAgentIds.value = assignAgentIds.value.filter((x) => x !== id);
  }
};

const toggleAssignGroup = (id, checked) => {
  if (checked) {
    if (!assignGroupIds.value.includes(id)) assignGroupIds.value.push(id);
  } else {
    assignGroupIds.value = assignGroupIds.value.filter((x) => x !== id);
  }
};

const handleAssignSubmit = async () => {
  assignSubmitting.value = true;
  try {
    const policyId = assignPolicy.value.id;
    await Promise.all([
      apiClient.post(`/policies/${policyId}/assign-agents`, assignAgentIds.value),
      apiClient.post(`/policies/${policyId}/assign-groups`, assignGroupIds.value),
    ]);
    ElMessage.success("Policy assigned successfully");
    assignDialogVisible.value = false;
  } catch (e) {
    ElMessage.error("Error assigning policy");
  } finally {
    assignSubmitting.value = false;
  }
};

onMounted(fetchData);

const getActionType = (action) => {
  const map = {
    block: "danger",
    allow_log: "warning",
    allow: "success",
  };
  return map[action?.toLowerCase()] || "info";
};

// The strongest rung's action (highest min_score) — shown as the list ACTION tag.
const topAction = (row) => {
  const acts = row.actions || [];
  if (!acts.length) return "allow";
  return [...acts].sort((a, b) => b.min_score - a.min_score)[0].action;
};

// Full ladder as a tooltip, e.g. "≥1 block · ≥0 allow_log".
const ladderSummary = (row) => {
  const acts = [...(row.actions || [])].sort((a, b) => b.min_score - a.min_score);
  if (!acts.length) return "no ladder";
  return acts.map((r) => `≥${r.min_score} ${r.action}`).join(" · ");
};
</script>

<style scoped>
.rule-section {
  background: #f8fafc;
  padding: 16px;
  border-radius: 8px;
  border: 1px solid #e2e8f0;
  margin-top: 10px;
}
.section-label {
  font-size: 13px;
  font-weight: 600;
  color: #64748b;
  margin-bottom: 12px;
}
.rule-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.channel-tag {
  margin-right: 4px;
}
.field-hint {
  margin-left: 12px;
  font-size: 12px;
  color: #94a3b8;
}
.field-hint-block {
  display: block;
  margin-left: 0;
  margin-top: 6px;
  line-height: 1.4;
}
.mt-4 {
  margin-top: 16px;
}
.pagination-container {
  margin-top: 24px;
  display: flex;
  justify-content: flex-end;
  padding-bottom: 8px;
}

.toolbar {
  display: flex;
  justify-content: space-between;
  margin-bottom: 20px;
}

.el-input__wrapper {
  background-color: #ffffff !important;
  box-shadow: 0 0 0 1px #e2e8f0 inset !important;
}

.el-input__wrapper.is-focus {
  box-shadow: 0 0 0 1px var(--primary-color) inset !important;
}
</style>
