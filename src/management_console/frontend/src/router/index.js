import { createRouter, createWebHistory } from "vue-router";
import MainLayout from "@/layouts/MainLayout.vue";

const routes = [
  {
    path: "/login",
    name: "Login",
    component: () => import("@/views/Login.vue"),
  },
  {
    path: "/",
    component: MainLayout,
    redirect: "/policies",
    children: [
      {
        path: "dashboard",
        name: "Dashboard",
        component: () => import("@/views/Dashboard.vue"),
      },
      {
        path: "agents",
        name: "Agents",
        component: () => import("@/views/Agents.vue"),
      },
      {
        path: "agent-groups",
        name: "AgentGroups",
        component: () => import("@/views/AgentGroups.vue"),
      },
      {
        path: "policies",
        name: "Policies",
        component: () => import("@/views/Policies.vue"),
      },
      {
        path: "alerts",
        name: "Alerts",
        component: () => import("@/views/Alerts.vue"),
      },
    ],
  },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

router.beforeEach((to, from, next) => {
  const token = localStorage.getItem("access_token");
  if (to.name !== "Login" && !token) {
    next({ name: "Login" });
  } else {
    next();
  }
});

export default router;
