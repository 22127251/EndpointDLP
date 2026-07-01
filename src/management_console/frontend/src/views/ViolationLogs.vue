<template>
  <div class="page-container">
    <div class="header-section">
      <div>
        <h1 class="title">Violation Logs</h1>
        <p class="subtitle">
          Notable endpoint decisions — policy blocks, monitored (allow_log) hits, and
          failure-mode outcomes — mirrored from each agent.
        </p>
      </div>
      <div class="header-actions">
        <el-dropdown @command="handleExport">
          <el-button :icon="Download">Export Logs</el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="json">Download JSON</el-dropdown-item>
              <el-dropdown-item command="yaml">Download YAML</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </div>

    <el-card shadow="never">
      <!-- TOOLBAR -->
      <div class="toolbar">
        <div class="filters">
          <el-input
            v-model="searchQuery"
            placeholder="Search decision / reason / channel"
            :prefix-icon="Search"
            clearable
            style="width: 300px"
            @input="handleSearch"
          />
          <el-select
            v-model="reasonFilter"
            placeholder="Filter by reason"
            clearable
            style="width: 210px"
            @change="onReasonChange"
          >
            <el-option v-for="r in reasonOptions" :key="r" :label="r" :value="r" />
          </el-select>
        </div>
        <el-button :icon="Refresh" @click="fetchData">Reload</el-button>
      </div>

      <el-table :data="logs" v-loading="loading" class="custom-table">
        <!-- EXPAND: matched policies + event metadata -->
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="expand-detail">
              <div class="detail-meta">
                <span><b>File:</b> {{ row.details?.name || "—" }}</span>
                <span><b>URL:</b> {{ row.details?.url || "—" }}</span>
                <span><b>Request:</b> {{ row.details?.req_id || "—" }}</span>
                <span><b>Elapsed:</b> {{ row.details?.elapsed_ms ?? "—" }} ms</span>
              </div>
              <el-table
                v-if="(row.matches || []).length"
                :data="row.matches"
                size="small"
                class="matches-table"
              >
                <el-table-column label="POLICY" min-width="200">
                  <template #default="{ row: m }">
                    {{ m.policy_name || "Deleted Policy" }}
                  </template>
                </el-table-column>
                <el-table-column label="ACTION" width="120">
                  <template #default="{ row: m }">
                    <el-tag :type="actionType(m.action)" size="small" effect="dark">
                      {{ (m.action || "").toUpperCase() }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="count" label="MATCHES" width="100" />
                <el-table-column prop="with_context" label="WITH CONTEXT" width="130" />
                <el-table-column label="CONTEXT WORDS" min-width="160">
                  <template #default="{ row: m }">
                    {{ (m.context_words_triggered || []).join(", ") || "—" }}
                  </template>
                </el-table-column>
              </el-table>
              <div v-else class="no-matches">
                No policy matched — failure-mode event (reason:
                {{ row.reason || "n/a" }}).
              </div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="TIME" width="180">
          <template #default="{ row }">
            {{ new Date(row.created_at).toLocaleString() }}
          </template>
        </el-table-column>

        <el-table-column label="AGENT" min-width="160">
          <template #default="{ row }">
            <span class="hostname">{{ row.agent_hostname || "Unknown" }}</span>
          </template>
        </el-table-column>

        <el-table-column label="CHANNEL" width="160">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{
              (row.channel || "").toUpperCase()
            }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column label="DECISION" width="120">
          <template #default="{ row }">
            <el-tag
              :type="row.decision === 'BLOCK' ? 'danger' : 'success'"
              effect="dark"
              size="small"
            >
              {{ row.decision }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="REASON" min-width="160">
          <template #default="{ row }">
            <el-tag
              v-if="row.reason"
              size="small"
              effect="plain"
              :type="row.reason === 'policy_violation' ? 'danger' : 'warning'"
            >
              {{ row.reason }}
            </el-tag>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>

        <el-table-column label="POLICIES" width="100" align="center">
          <template #default="{ row }">{{ (row.matches || []).length }}</template>
        </el-table-column>
      </el-table>

      <!-- Pagination -->
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
  </div>
</template>

<script setup>
import { ref, onMounted } from "vue";
import { Download, Search, Refresh } from "@element-plus/icons-vue";
import apiClient from "@/api/axios";
import { exportData } from "@/utils/exporter";

const loading = ref(false);
const logs = ref([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(20);

const searchQuery = ref("");
const reasonFilter = ref("");
const reasonOptions = [
  "policy_violation",
  "oversize",
  "text_cap",
  "unsupported_format",
  "timeout",
  "analysis_error",
  "malformed",
];

const actionType = (a) =>
  ({ block: "danger", allow_log: "warning", allow: "success" }[
    a?.toLowerCase()
  ] || "info");

let searchTimer = null;
const handleSearch = () => {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    page.value = 1;
    fetchData();
  }, 500);
};

const onReasonChange = () => {
  page.value = 1;
  fetchData();
};

const fetchData = async () => {
  loading.value = true;
  try {
    const res = await apiClient.get("/violation-logs/", {
      params: {
        page: page.value,
        page_size: pageSize.value,
        search: searchQuery.value || undefined,
        reason: reasonFilter.value || undefined,
      },
    });
    logs.value = res.data.items || [];
    total.value = res.data.total || 0;
  } finally {
    loading.value = false;
  }
};

const handleExport = (fmt) => {
  exportData(logs.value, `violation_logs_p${page.value}`, fmt);
};

onMounted(fetchData);
</script>

<style scoped>
.expand-detail {
  padding: 8px 48px;
}
.detail-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
  margin-bottom: 12px;
  font-size: 13px;
  color: #475569;
}
.matches-table {
  width: 100%;
}
.no-matches {
  font-size: 13px;
  color: #94a3b8;
  font-style: italic;
}
.hostname {
  font-weight: 600;
  color: #1e293b;
}
.muted {
  color: #cbd5e1;
}
.filters {
  display: flex;
  gap: 10px;
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
