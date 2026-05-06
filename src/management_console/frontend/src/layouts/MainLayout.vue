<template>
  <el-container class="main-layout">
    <!-- Sidebar -->
    <el-aside width="240px" class="sidebar">
      <div class="logo-section">
        <el-icon :size="24" color="#0d8a94"><Shield /></el-icon>
        <span class="logo-text">ENDPOINT DLP</span>
      </div>

      <el-menu
        :default-active="$route.path"
        router
        class="sidebar-menu"
        background-color="#ffffff"
        text-color="#64748b"
        active-text-color="#0d8a94"
      >
        <!-- <el-menu-item index="/dashboard">
          <el-icon><Menu /></el-icon>
          <span>Dashboard</span>
        </el-menu-item> -->

        <el-menu-item index="/policies">
          <el-icon><Lock /></el-icon>
          <span>Policies</span>
        </el-menu-item>

        <!-- <el-menu-item index="/alerts">
          <el-icon><Warning /></el-icon>
          <span>Alerts & Logs</span>
        </el-menu-item> -->

        <el-menu-item index="/agents">
          <el-icon><Monitor /></el-icon>
          <span>Agents</span>
        </el-menu-item>

        <el-menu-item index="/agent-groups">
          <el-icon><Files /></el-icon>
          <span>Agent Groups</span>
        </el-menu-item>
      </el-menu>

      <div class="sidebar-footer">
        <el-menu class="sidebar-menu" router>
          <!-- <el-menu-item index="/settings">
            <el-icon><Setting /></el-icon>
            <span>Settings</span>
          </el-menu-item> -->
          <el-menu-item @click="handleLogout">
            <el-icon><SwitchButton /></el-icon>
            <span>Logout</span>
          </el-menu-item>
        </el-menu>
      </div>
    </el-aside>

    <el-container>
      <!-- Header -->
      <el-header class="header">
        <div class="header-search">
          <el-input
            :prefix-icon="Search"
            placeholder="Search policies, agents..."
            style="width: 400px"
          />
        </div>
        <div class="header-user">
          <el-divider direction="vertical" />
          <div class="user-info">
            <span class="user-name">Admin User</span>
            <span class="user-role">Security Lead</span>
          </div>
          <el-avatar
            :size="32"
            src="https://cube.elemecdn.com/0/88/03b0d39583f48206768a7534e55bcpng.png"
          />
        </div>
      </el-header>

      <!-- Main Content -->
      <el-main class="content-area">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { Search } from "@element-plus/icons-vue";
import { useRouter } from "vue-router";

const router = useRouter();

const handleLogout = () => {
  localStorage.removeItem("access_token");
  router.push("/login");
};
</script>

<style scoped>
.main-layout {
  height: 100vh;
}
.sidebar {
  background: #fff;
  border-right: 1px solid #e2e8f0;
  display: flex;
  flex-direction: column;
}
.logo-section {
  padding: 24px;
  display: flex;
  align-items: center;
  gap: 10px;
  border-bottom: 1px solid #f1f5f9;
}
.logo-text {
  font-weight: 800;
  font-size: 18px;
  color: #1e293b;
  letter-spacing: -0.5px;
}
.sidebar-menu {
  border-right: none;
  flex: 1;
}
.divider-text {
  font-size: 10px;
  color: #94a3b8;
  font-weight: bold;
}
.sidebar-footer {
  border-top: 1px solid #f1f5f9;
  padding: 10px 0;
}

.header {
  background: #fff;
  border-bottom: 1px solid #e2e8f0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
}
.header-user {
  display: flex;
  align-items: center;
  gap: 15px;
}
.user-info {
  display: flex;
  flex-direction: column;
  text-align: right;
}
.user-name {
  font-size: 13px;
  font-weight: 600;
  color: #1e293b;
}
.user-role {
  font-size: 11px;
  color: #64748b;
}

.content-area {
  background-color: #f8fafc;
  padding: 0;
}

:deep(.el-menu-item.is-active) {
  border-left: 3px solid #0d8a94;
  background-color: #f0fdfa !important;
}
</style>
