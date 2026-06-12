import yaml from "js-yaml";

export const exportData = (data, fileName, format = "json") => {
  let content = "";
  let mimeType = "";

  if (format === "yaml") {
    content = yaml.dump(data);
    mimeType = "text/yaml";
  } else {
    content = JSON.stringify(data, null, 2);
    mimeType = "application/json";
  }

  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${fileName}.${format}`;
  link.click();
  URL.revokeObjectURL(url);
};
