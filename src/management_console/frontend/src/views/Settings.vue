<template>
  <div class="page-container settings-page">
    <!-- TOP SEARCH BAR -->
    <div class="settings-search-container">
      <el-input
        v-model="searchQuery"
        placeholder="Search settings..."
        :prefix-icon="Search"
        clearable
        class="search-input"
      />
    </div>

    <el-container class="settings-main-container">
      <!-- SIDEBAR: CATEGORIES -->
      <el-aside width="220px" class="settings-sidebar">
        <el-menu
          :default-active="activeCategory"
          @select="handleCategorySelect"
          class="settings-menu"
        >
          <el-menu-item index="commonly">Commonly Used</el-menu-item>
          <el-menu-item index="server">Server Config</el-menu-item>
          <el-menu-item index="agent">Agent Behavior</el-menu-item>
          <el-menu-item index="security">Security & Privacy</el-menu-item>
        </el-menu>
      </el-aside>

      <!-- MAIN CONTENT: SCROLLABLE LIST -->
      <el-main class="settings-content">
        <div
          v-for="cat in filteredSchema"
          :key="cat.id"
          :id="`section-${cat.id}`"
          class="settings-section"
        >
          <h2 class="section-title">{{ cat.title }}</h2>

          <div
            v-for="item in cat.settings"
            :key="item.key"
            class="setting-item"
          >
            <div class="setting-info">
              <div class="setting-label">
                {{ cat.id }}: <span class="label-bold">{{ item.label }}</span>
              </div>
              <div class="setting-description">{{ item.description }}</div>
            </div>

            <div class="setting-control">
              <el-input
                v-if="item.type === 'text'"
                v-model="rawSettings[item.key]"
                @change="(val) => markDirty(item.key, val)"
              />
              <el-input-number
                v-if="item.type === 'number'"
                v-model="rawSettings[item.key]"
                style="width: 150px"
                @change="(val) => markDirty(item.key, val)"
              />
              <el-select
                v-if="item.type === 'select'"
                v-model="rawSettings[item.key]"
                style="width: 100%"
                @change="(val) => markDirty(item.key, val)"
              >
                <el-option
                  v-for="opt in item.options"
                  :key="opt"
                  :label="opt"
                  :value="opt"
                />
              </el-select>
              <el-checkbox
                v-if="item.type === 'boolean'"
                v-model="rawSettings[item.key]"
                @change="(val) => markDirty(item.key, val)"
              >
                Enable
              </el-checkbox>
            </div>
          </div>
        </div>

        <transition name="el-fade-in">
          <div v-if="isDirty" class="save-footer">
            <span class="save-hint">You have unsaved changes</span>
            <el-button type="primary" @click="saveSettings" :loading="saving"
              >Save Settings</el-button
            >
          </div>
        </transition>
      </el-main>
    </el-container>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from "vue";
import { Search } from "@element-plus/icons-vue";
import apiClient from "@/api/axios";
import { ElMessage } from "element-plus";

const loading = ref(false);
const saving = ref(false);
const isDirty = ref(false);
const searchQuery = ref("");
const activeCategory = ref("commonly");

const rawSettings = ref({});

const settingsSchema = [
  {
    id: "commonly",
    title: "Commonly Used",
    settings: [
      {
        key: "HEARTBEAT_INTERVAL_SECONDS",
        label: "Heartbeat Interval",
        description:
          "Controls how often (in seconds) the agent check-in with the server.",
        type: "number",
      },
      {
        key: "LOG_RETENTION_DAYS",
        label: "Log Retention Days",
        description:
          "Controls how many days of logs are retained on the agent.",
        type: "number",
      },
      {
        key: "AUTO_CLEAN_UP_LOG",
        label: "Auto Clean Up Logs",
        description:
          "If enabled, the agent will automatically delete logs older than the retention period.",
        type: "boolean",
        options: [true, false],
      },
      // {
      //   key: "log_level",
      //   label: "Log Level",
      //   description: "Defines the verbosity of the agent logs.",
      //   type: "select",
      //   options: ["info", "debug", "warning", "error"],
      // },
    ],
  },
  // {
  //   id: "server",
  //   title: "Server Config",
  //   settings: [
  //     {
  //       key: "server_url",
  //       label: "Server URL",
  //       description: "The public endpoint URL that agents use to communicate.",
  //       type: "text",
  //     },
  //     {
  //       key: "maintenance_mode",
  //       label: "Maintenance Mode",
  //       description: "If enabled, agents will stop sending logs temporarily.",
  //       type: "boolean",
  //     },
  //   ],
  // },
];
const modifiedSettings = ref({});

const filteredSchema = computed(() => {
  if (!searchQuery.value) return settingsSchema;

  const query = searchQuery.value.toLowerCase();
  return settingsSchema
    .map((cat) => ({
      ...cat,
      settings: cat.settings.filter(
        (s) =>
          s.label.toLowerCase().includes(query) ||
          s.description.toLowerCase().includes(query),
      ),
    }))
    .filter((cat) => cat.settings.length > 0);
});

const fetchData = async () => {
  loading.value = true;
  try {
    const res = await apiClient.get("/settings/settings");
    rawSettings.value = res.data;
    console.log("Fetched settings:", rawSettings.value);
    isDirty.value = false;
  } finally {
    loading.value = false;
  }
};

const saveSettings = async () => {
  if (Object.keys(modifiedSettings.value).length === 0) return;

  saving.value = true;
  console.log("Modified settings to save:", modifiedSettings);
  // const payload = {
  //   settings: {
  //     HEARTBEAT_INTERVAL_SECONDS: Number(
  //       rawSettings.value.HEARTBEAT_INTERVAL_SECONDS,
  //     ),
  //     LOG_RETENTION_DAYS: Number(rawSettings.value.LOG_RETENTION_DAYS),
  //     AUTO_CLEAN_UP_LOG: Boolean(rawSettings.value.AUTO_CLEAN_UP_LOG),
  //   },
  // };
  const payload = {
    settings: {},
  };

  console.log("modified settings:", modifiedSettings.value);

  for (const key in modifiedSettings.value) {
    let value = modifiedSettings.value[key];
    if (typeof value === "string") {
      // Try to parse numbers and booleans
      if (!isNaN(value)) {
        value = Number(value);
      } else if (value.toLowerCase() === "true") {
        value = true;
      } else if (value.toLowerCase() === "false") {
        value = false;
      }
    }
    payload.settings[key] = value;
  }

  console.log("Final payload to save:", payload);

  try {
    await apiClient.patch("/settings/settings", payload);
    ElMessage.success("Settings saved successfully");
    isDirty.value = false;
  } catch (error) {
    console.error("Error Detail:", error.response?.data);
    ElMessage.error(error.response?.data?.detail[0]?.msg || "Update failed");
  } finally {
    saving.value = false;
  }
};

const markDirty = (key, value) => {
  modifiedSettings.value[key] = value;
  isDirty.value = true;
};

const handleCategorySelect = (id) => {
  const el = document.getElementById(`section-${id}`);
  if (el) el.scrollIntoView({ behavior: "smooth" });
};

onMounted(fetchData);
</script>

<style scoped>
.settings-page {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 100px);
  padding: 0 !important;
}

.settings-search-container {
  padding: 20px 40px;
  background: #fff;
  border-bottom: 1px solid #e2e8f0;
}
.search-input {
  width: 100%;
  max-width: 800px;
}

.settings-main-container {
  flex: 1;
  overflow: hidden;
}

.settings-sidebar {
  background: #fff;
  border-right: 1px solid #e2e8f0;
}
.settings-menu {
  border-right: none;
}

.settings-content {
  background: #fff;
  padding: 20px 60px;
  scroll-behavior: smooth;
}

.settings-section {
  margin-bottom: 50px;
}
.section-title {
  font-size: 22px;
  font-weight: 600;
  margin-bottom: 30px;
  color: #1e293b;
}

.setting-item {
  margin-bottom: 30px;
  max-width: 800px;
  position: relative;
  padding-left: 15px;
}
.setting-item::before {
  content: "";
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 2px;
  background: transparent;
}
.setting-item:hover::before {
  background: var(--primary-color);
}

.setting-info {
  margin-bottom: 10px;
}
.setting-label {
  font-size: 13px;
  color: var(--text-secondary);
}
.label-bold {
  font-weight: 700;
  color: #1e293b;
  font-size: 14px;
}
.setting-description {
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.5;
  margin-top: 4px;
}

.setting-control {
  max-width: 400px;
}

.save-footer {
  position: fixed;
  bottom: 30px;
  right: 60px;
  background: #fff;
  padding: 15px 25px;
  border-radius: 8px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  display: flex;
  align-items: center;
  gap: 20px;
  z-index: 100;
  border: 1px solid var(--primary-color);
}
.save-hint {
  font-size: 13px;
  color: var(--primary-color);
  font-weight: 600;
}
</style>
