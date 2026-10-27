// HIL i18n: Simplified Chinese layer for the static landing page.
// - Static copy ships as bilingual spans in index.html; html.zh shows the zh spans (styles.css).
// - Dynamic strings (bench scenes, tabs, prompts, console) translate through t() in bench.js.
// - An inline script in <head> sets html.zh + lang before first paint; this file owns the
//   toggle, the dictionary, attribute/meta translation and the localStorage override.
(function () {
  var STORE = "hil-lang";
  var root = document.documentElement;

  // en -> zh-CN dictionary. Keys are the exact English strings; bench.js and the
  // attribute walker look up through t(). Commands, slugs and readouts stay English.
  var ZH = {
    // chrome
    "copy": "复制",
    "copied": "已复制",
    "Workbenches": "工作台",
    " workbench": "工作台",
    "fig ": "图 ",
    // meta
    "HIL: Hardware-in-the-Loop testing for lab instruments": "HIL：面向实验室仪器的硬件在环测试",
    "Hardware-in-the-loop testing for lab instruments: 725 SCPI devices across 7 vendors, plus power profilers, logic analyzers and debug probes. Safety-checked, agent-ready.": "面向实验室仪器的硬件在环测试：覆盖 7 家厂商的 725 台 SCPI 设备，外加功耗分析仪、逻辑分析仪与调试探头。安全检查到位，智能体开箱即用。",
    "One Python harness that drives 725 SCPI instruments across 7 vendors, plus power profilers, logic analyzers and flash/debug probes, all safety-checked and agent-ready.": "一个 Python 框架，驱动 7 家厂商的 725 台 SCPI 仪器，外加功耗分析仪、逻辑分析仪和烧录/调试探头，全部经过安全检查，智能体开箱即用。",
    // aria labels
    "Example terminal session": "示例终端会话",
    "Primary": "主导航",
    "SCPI bench workbench": "SCPI 工作台",
    "Flash and debug workbench": "烧录与调试工作台",
    "Logic analyzer workbench": "逻辑分析仪工作台",
    "Power profiling workbench": "功耗分析工作台",
    "Debug loop diagram": "调试循环示意图",
    "Footer": "页脚",
    "Switch language": "切换语言",
    // loop-step illustration alt text
    "Isometric illustration of firmware compiling on a host slab": "固件在主机板上编译的等距示意图",
    "Isometric illustration of a probe pod writing a chip over a ribbon cable": "探头座经排线写入芯片的等距示意图",
    "Isometric illustration of a chip halted under a hover-lens": "芯片在悬停镜头下停机的等距示意图",
    "Isometric illustration of memory slices spooled onto an artifact tray": "内存切片写入产物托盘的等距示意图",
    "Isometric illustration of a bench instrument probing a chip": "台架仪器探测芯片的等距示意图",
    // bench scenes: names, tags, hints, legend keys
    "SCPI bench": "SCPI 工作台",
    "oscilloscope, SMU and supply over LAN, serial or GPIB": "示波器、源表与电源，经 LAN、串口或 GPIB",
    "Hover a part to read it": "悬停任意部件即可查看说明",
    "the agent and the commands it sends": "智能体及其发出的命令",
    "an instrument and its readings": "仪器及其读数",
    "safety policy: pass": "安全策略：通过",
    "Flash and debug": "烧录与调试",
    "build, flash, halt, step, RTT over SWD, JTAG or serial": "构建、烧录、停机、单步、RTT，经 SWD、JTAG 或串口",
    "the agent and the firmware it flashes": "智能体及其烧录的固件",
    "a probe, a target and its wire": "探头、目标板及其连线",
    "verify + hash recorded": "校验 + 哈希已记录",
    "Logic analyzer": "逻辑分析仪",
    "flash the driver over SWD, trigger on GPIO, decode I2C to frames": "经 SWD 烧录驱动，GPIO 触发，把 I2C 解码成帧",
    "the agent, the GPIO trigger and live frames": "智能体、GPIO 触发与实时帧",
    "analyzer, probe bundle, SWD ribbon and I2C taps": "分析仪、探头组、SWD 排线与 I2C 窃听点",
    "capture written to the spool": "捕获已写入 spool",
    "Power profiling": "功耗分析",
    "source and measure current, log a soak, integrate energy": "供电并测量电流，记录 soak 日志，积分能耗",
    "the agent and the source setpoint": "智能体与电源设定点",
    "PPK2, the DUT and its digital inputs": "PPK2、DUT 及其数字输入",
    "average draw within budget": "平均电流在预算之内",
    // bench scenes: prompts
    "Set the Keithley 2400 to source 3.3 V with a 100 mA limit, then ask the Rigol scope whether the rail settles within 10 mV before the DUT powers up. Use hil_exec so the safety policy sees every command.": "把 Keithley 2400 设为输出 3.3 V、限流 100 mA，然后在 DUT 上电前询问 Rigol 示波器电源轨是否稳定在 10 mV 以内。使用 hil_exec，让安全策略看到每一条命令。",
    "Sweep the EA supply from 0 to 5 V in 100 mV steps, read back at each step, drain the error queue with hil_errors, and plot the linearity error from the returned JSON.": "把 EA 电源从 0 到 5 V 以 100 mV 步进扫描，每一步回读，用 hil_errors 清空错误队列，并根据返回的 JSON 绘制线性度误差曲线。",
    "Build the firmware in fw/, flash it to the nRF52840 DK with hil_debug, then halt at main and single-step through init until the status LED blinks. Report the program counter and any hard fault.": "构建 fw/ 中的固件，用 hil_debug 烧录到 nRF52840 DK，然后在 main 处停机并单步执行 init，直到状态 LED 闪烁。报告程序计数器和任何硬件错误。",
    "Once flashing verifies, open an RTT console with hil_debug and watch ten seconds of log for assertions before calling the run good.": "烧录校验通过后，用 hil_debug 打开 RTT 控制台，观察十秒日志确认没有断言失败，再判定本次运行成功。",
    "Flash the SSD1306 display driver to the DUT with hil_debug over SWD, then capture two seconds of SCL, SDA and the GPIO0 trigger at 4 MS/s and decode every frame.": "用 hil_debug 经 SWD 把 SSD1306 显示驱动烧录到 DUT，然后以 4 MS/s 采集 SCL、SDA 和 GPIO0 触发信号各两秒，并解码每一帧。",
    "Gate the capture on the DUT trigger edge, then check the decoded init sequence against the panel datasheet: address, control bytes, and any frame that NACKs.": "以 DUT 触发沿作为采集门控，然后把解码出的初始化序列与面板数据手册核对：地址、控制字节，以及任何 NACK 的帧。",
    "Build a power bench with the PPK2 as the 3V3 source for the DUT. Stream one hour of current with hil_capture into the spool, then report mean, p95 and total energy in Wh.": "用 PPK2 作为 DUT 的 3V3 电源搭建功耗测试台。用 hil_capture 把一小时的电流流式写入 spool，然后报告均值、p95 和以 Wh 计的总能耗。",
    "Design and flash a low-power firmware build, re-run the same one-hour profile, and score it against the baseline capture in mean current and estimated battery days.": "设计并烧录一版低功耗固件，重新运行同样的一小时功耗分析，并以平均电流和预估电池天数对照基线采集进行评分。",
    // bench scenes: sessions
    "Set the Keithley 2400 to 3.3 V with a 100 mA limit, then verify the rail on the scope.": "把 Keithley 2400 设为 3.3 V、限流 100 mA，然后在示波器上验证电源轨。",
    "profile loaded: SMU, driver scpi, safety policy pass": "档案已加载：SMU，驱动 scpi，安全策略通过",
    "ok, error queue empty": "ok，错误队列为空",
    "3.298 V, within 10 mV: DUT clear to power": "3.298 V，误差在 10 mV 内：DUT 可以上电",
    "Flash fw/app.elf to the nRF52840 DK, step through init, and watch for faults.": "把 fw/app.elf 烧录到 nRF52840 DK，单步执行 init，并留意错误。",
    "1 probe: J-Link, serial 000687745590": "1 个探头：J-Link，序列号 000687745590",
    "verified, hash 9c41e2 recorded": "校验通过，哈希 9c41e2 已记录",
    "PC 0x00002A18, no hard fault, log clean": "PC 0x00002A18，无硬件错误，日志干净",
    "Flash the display driver, capture I2C gated on the GPIO trigger, and validate the init sequence.": "烧录显示驱动，以 GPIO 触发为门控采集 I2C，并验证初始化序列。",
    "verified, SWD at 4 MHz": "校验通过，SWD 速率 4 MHz",
    "8.0 M samples, 14 trigger edges": "800 万样本，14 个触发边沿",
    "128 frames to 0x3c, init matches the datasheet": "128 帧发往 0x3c，初始化与数据手册一致",
    "trigger to first SCL edge 412 us, stable across runs": "触发到第一个 SCL 边沿 412 us，多次运行保持稳定",
    "Source 3V3 from the PPK2 and log one hour of current on the real device.": "用 PPK2 输出 3V3，在真实设备上记录一小时电流。",
    "backend ready, ppk2-api 0.9.2": "后端就绪，ppk2-api 0.9.2",
    "NDJSON spool written, 3.6 M samples": "NDJSON spool 已写入，360 万样本",
    "mean 52.3 mA, p95 81.0 mA, 0.9 Wh": "均值 52.3 mA，p95 81.0 mA，0.9 Wh",
    "baseline saved, ready to A/B the firmware": "基线已保存，可以对固件做 A/B 对比",
    // bench scenes: parts
    "coding agent": "编码智能体",
    "Claude Code, Codex, Cursor or Qoder over MCP": "Claude Code、Codex、Cursor 或 Qoder，经 MCP",
    "write, query, safe_execute, capture": "write、query、safe_execute、capture",
    "SourceMeter (SMU), driver: scpi": "源表（SMU），驱动：scpi",
    "Oscilloscope, driver: scpi": "示波器，驱动：scpi",
    "DC power supply, driver: scpi": "直流电源，驱动：scpi",
    "device under test": "被测设备",
    "tcp:// serial:// visa:// - one byte-pipe": "tcp:// serial:// visa:// - 一条字节管道",
    "builds, flashes and steps through main": "构建、烧录，并单步执行 main",
    "probe-rs, pyOCD, OpenOCD, esptool, stlink": "probe-rs、pyOCD、OpenOCD、esptool、stlink",
    "debug probe": "调试探头",
    "debug://probe-rs, SWD or JTAG": "debug://probe-rs，SWD 或 JTAG",
    "nRF52840 target, flash + halt + step": "nRF52840 目标，烧录 + 停机 + 单步",
    "serial ROM bootloader, no probe": "串口 ROM 引导程序，无需探头",
    "RTT + GDB": "RTT + GDB",
    "SEGGER RTT logs and a GDB session": "SEGGER RTT 日志与 GDB 会话",
    "on-chip flash and debug, out of process": "片上烧录与调试，独立进程",
    "reads decoded frames as JSON": "以 JSON 读取解码后的帧",
    "in-process, no daemon, no GPL boundary": "进程内运行，无守护进程，无 GPL 边界",
    "logic://usb, 4 MS/s on ch0..ch2, trigger on ch2": "logic://usb，ch0..ch2 以 4 MS/s 采样，ch2 触发",
    "probe-rs over SWD, flashes the driver": "probe-rs 经 SWD，烧录驱动",
    "I2C master, GPIO0 pulses once per frame": "I2C 主机，GPIO0 每帧脉冲一次",
    "i2c display": "I2C 显示屏",
    "SSD1306 panel at 0x3c, bus tapped": "SSD1306 面板，地址 0x3c，总线已窃听",
    "CaptureArtifact: path + metadata, not bytes": "CaptureArtifact：路径 + 元数据，而非字节",
    "decode": "解码",
    "sigrok i2c -> 128 frames": "sigrok i2c -> 128 帧",
    "captures land in a TTL-managed spool": "捕获落入 TTL 管理的 spool",
    "plans the soak, scores the firmware": "规划 soak 测试，为固件评分",
    "Sample records -> summarize(), energy int(P dt)": "Sample 记录 -> summarize()，能耗 int(P dt)",
    "Power Profiler Kit II, driver: ppk2": "Power Profiler Kit II，驱动：ppk2",
    "powered at 3.3 V, current measured": "以 3.3 V 供电，测量电流",
    "soak log": "soak 日志",
    "append-only NDJSON, 0.9 Wh integrated": "只追加 NDJSON，积分得 0.9 Wh",
    "digital in": "数字输入",
    "8 input channels D0..D7 (inputs only)": "8 路输入通道 D0..D7（仅输入）",
    "ppk2:// - pseudo-SCPI over ppk2-api": "ppk2:// - 基于 ppk2-api 的伪 SCPI"
  };

  function t(s) {
    if (root.classList.contains("zh") && s) {
      var v = ZH[String(s)];
      if (v != null) return v;
    }
    return s;
  }

  // attribute + meta translation for the static document
  var ATTRS = [
    ["img.loop-art", "alt"],
    [".iso-plate", "aria-label"],
    [".term", "aria-label"],
    ["nav[aria-label]", "aria-label"],
    ['meta[name="description"]', "content"],
    ['meta[property="og:title"]', "content"],
    ['meta[property="og:description"]', "content"],
    ['meta[name="twitter:title"]', "content"],
    ['meta[name="twitter:description"]', "content"]
  ];
  function norm(s) { return String(s).replace(/\s+/g, " ").trim(); }
  function applyAttrs(zh) {
    ATTRS.forEach(function (pair) {
      document.querySelectorAll(pair[0]).forEach(function (el) {
        var o = el.__i18nAttr || (el.__i18nAttr = {});
        if (o[pair[1]] == null) o[pair[1]] = el.getAttribute(pair[1]);
        el.setAttribute(pair[1], zh ? (ZH[norm(o[pair[1]])] || o[pair[1]]) : o[pair[1]]);
      });
    });
    var oTitle = document.__i18nTitle || (document.__i18nTitle = document.title);
    document.title = zh ? (ZH[norm(oTitle)] || oTitle) : oTitle;
  }

  // loop-step readout in bench.js concatenates "label: detail"
  function tReadout(label, detail) {
    return t(label) + ": " + t(detail);
  }

  function detect() {
    try {
      var saved = localStorage.getItem(STORE);
      if (saved === "zh" || saved === "en") return saved;
    } catch (e) {}
    var langs = navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language || ""];
    for (var i = 0; i < langs.length; i++) {
      if (/^zh\b|^zh-/i.test(langs[i])) return "zh";
    }
    return "en";
  }

  function render(lang) {
    var zh = lang === "zh";
    root.classList.toggle("zh", zh);
    root.setAttribute("lang", zh ? "zh-CN" : "en");
    applyAttrs(zh);
    // keep every static copy button's label in sync (bench prompts re-render themselves)
    document.querySelectorAll(".copy-btn").forEach(function (b) {
      if (!b.hasAttribute("data-copied")) b.textContent = t("copy");
    });
    var btn = document.querySelector("[data-lang-toggle]");
    if (btn) btn.setAttribute("aria-pressed", zh ? "true" : "false");
    document.dispatchEvent(new CustomEvent("hil:lang", { detail: { lang: zh ? "zh" : "en" } }));
  }

  function setLang(lang) {
    try { localStorage.setItem(STORE, lang); } catch (e) {}
    render(lang);
  }

  window.__HIL_I18N = {
    t: t,
    tReadout: tReadout,
    zh: function () { return root.classList.contains("zh"); },
    set: setLang
  };

  function init() {
    var btn = document.querySelector("[data-lang-toggle]");
    if (btn) {
      btn.addEventListener("click", function () {
        setLang(root.classList.contains("zh") ? "en" : "zh");
      });
    }
    render(detect());
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
