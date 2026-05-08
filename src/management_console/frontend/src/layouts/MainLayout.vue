<template>
  <el-container class="main-layout">
    <!-- Sidebar -->
    <el-aside width="240px" class="sidebar">
      <div class="logo-section">
        <span class="logo-text">OURDLP</span>
      </div>

      <el-menu
        :default-active="$route.path"
        router
        class="sidebar-menu"
        background-color="#ffffff"
        text-color="#64748b"
        active-text-color="#0d8a94"
      >
        <div class="sidebar-group-label">GENERAL</div>
        <!-- <el-menu-item index="/dashboard">
          <el-icon><Menu /></el-icon>
          <span>Dashboard</span>
        </el-menu-item> -->

        <el-menu-item index="/policies">
          <el-icon><Lock /></el-icon>
          <span>Policies</span>
        </el-menu-item>

        <el-menu-item index="/agents">
          <el-icon><Monitor /></el-icon>
          <span>Agents</span>
        </el-menu-item>

        <el-menu-item index="/agent-groups">
          <el-icon><Files /></el-icon>
          <span>Agent Groups</span>
        </el-menu-item>

        <template v-if="auth.isAdmin">
          <div class="sidebar-group-label admin-label">ADMINISTRATION</div>

          <el-menu-item index="/violation-log">
            <el-icon><Warning /></el-icon>
            <span>Violation Logs</span>
          </el-menu-item>

          <el-menu-item index="/users">
            <el-icon><User /></el-icon>
            <span>User Management</span>
          </el-menu-item>
        </template>
      </el-menu>

      <div class="sidebar-footer">
        <el-menu class="sidebar-menu" router>
          <el-menu-item v-if="auth.isAdmin" index="/settings">
            <el-icon><Setting /></el-icon>
            <span>Settings</span>
          </el-menu-item>
          <el-menu-item @click="handleLogout">
            <el-icon><SwitchButton /></el-icon>
            <span>Logout</span>
          </el-menu-item>
        </el-menu>
      </div>
    </el-aside>

    <el-container>
      <el-header class="header">
        <div class="header-left">
          <!-- BREADCRUMBS -->
          <el-breadcrumb separator="/">
            <el-breadcrumb-item :to="{ path: '/' }">Home</el-breadcrumb-item>
            <el-breadcrumb-item v-if="$route.name">{{
              $route.name
            }}</el-breadcrumb-item>
          </el-breadcrumb>
        </div>

        <div class="header-right">
          <el-divider direction="vertical" />

          <!-- USER PROFILE CLICKABLE -->
          <div class="user-profile" @click="$router.push('/profile')">
            <div class="user-info">
              <span class="user-name">{{ auth.userDisplayName }}</span>
              <span class="user-role">{{ auth.userRoleLabel }}</span>
            </div>
            <el-avatar
              :size="32"
              src="https://cube.elemecdn.com/0/88/03b0d39583f48206768a7534e55bcpng.png"
            />
          </div>
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
import {
  Menu,
  Warning,
  Lock,
  Monitor,
  Files,
  User,
  Setting,
  SwitchButton,
} from "@element-plus/icons-vue";
import { useRouter } from "vue-router";
import { useAuthStore } from "@/store/auth";
const auth = useAuthStore();

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

.header-right {
  display: flex;
  align-items: center;
  gap: 20px;
}

.user-profile {
  display: flex;
  align-items: center;
  gap: 12px;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 6px;
  transition: all 0.2s;
}
.user-profile:hover {
  background-color: #f1f5f9;
}
.user-info {
  display: flex;
  flex-direction: column;
  text-align: right;
}
.user-name {
  font-size: 13px;
  font-weight: 700;
  color: #1e293b;
}
.user-role {
  font-size: 11px;
  color: #94a3b8;
}

.content-area {
  background-color: #f8fafc;
  padding: 0;
}

:deep(.el-menu-item.is-active) {
  border-left: 3px solid #0d8a94;
  background-color: #f0fdfa !important;
}

.sidebar-group-label {
  padding: 20px 20px 10px;
  font-size: 11px;
  font-weight: 700;
  color: #94a3b8;
  letter-spacing: 1px;
}
.admin-label {
  color: #f87171;
}
</style>
