<template>
  <div class="page-container">
    <div class="header-section">
      <div>
        <h1 class="title">Server Settings</h1>
        <p class="subtitle">
          Configure global parameters for the DLP environment.
        </p>
      </div>
    </div>

    <el-card shadow="never" style="max-width: 800px">
      <el-tabs v-model="activeTab">
        <el-tab-pane label="General Configuration" name="general">
          <el-form :model="settings" label-position="top" v-loading="loading">
            <el-row :gutter="20">
              <el-col :span="12">
                <el-form-item label="Agent Heartbeat Interval (seconds)">
                  <el-input-number
                    v-model="settings.heartbeat_interval"
                    :min="10"
                    :max="3600"
                    style="width: 100%"
                  />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="Violation Log Retention (days)">
                  <el-input-number
                    v-model="settings.log_retention_days"
                    :min="1"
                    :max="365"
                    style="width: 100%"
                  />
                </el-form-item>
              </el-col>
            </el-row>

            <el-form-item label="Server Endpoint URL">
              <el-input
                v-model="settings.server_url"
                placeholder="https://dlp-api.company.com"
              />
            </el-form-item>

            <el-form-item label="Global Alert Email">
              <el-input
                v-model="settings.alert_email"
                placeholder="security-alerts@company.com"
              />
            </el-form-item>

            <el-divider />

            <div style="text-align: right">
              <el-button :icon="Refresh" @click="fetchSettings"
                >Reset</el-button
              >
              <el-button
                type="primary"
                @click="handleUpdate"
                :loading="updating"
                >Save Settings</el-button
              >
            </div>
          </el-form>
        </el-tab-pane>

        <el-tab-pane label="Security & Keys" name="security">
          <p class="text-muted">
            Manage encryption keys and agent authentication keys.
          </p>
          <el-form label-position="top">
            <el-form-item label="Master Encryption Key">
              <el-input v-model="dummyKey" show-password disabled>
                <template #append>
                  <el-button>Rotate Key</el-button>
                </template>
              </el-input>
            </el-form-item>
          </el-form>
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from "vue";
import { Refresh } from "@element-plus/icons-vue";
import apiClient from "@/api/axios";
import { ElMessage } from "element-plus";

const activeTab = ref("general");
const loading = ref(false);
const updating = ref(false);
const dummyKey = ref("dlp_master_key_v1_secret_xxxxxxxx");

const settings = ref({
  heartbeat_interval: 60,
  log_retention_days: 30,
  server_url: "",
  alert_email: "",
});

const fetchSettings = async () => {
  loading.value = true;
  try {
    const res = await apiClient.get("/settings/settings");
    settings.value = res.data;
  } finally {
    loading.value = false;
  }
};

const handleUpdate = async () => {
  updating.value = true;
  try {
    // API: PATCH /api/v1/settings/settings
    await apiClient.patch("/settings/settings", settings.value);
    ElMessage.success("System settings updated successfully");
    fetchSettings();
  } catch (error) {
    ElMessage.error("Failed to update settings");
  } finally {
    updating.value = false;
  }
};

onMounted(fetchSettings);
</script>
