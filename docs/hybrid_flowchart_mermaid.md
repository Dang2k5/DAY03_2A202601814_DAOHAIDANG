---
config:
  flowchart:
    curve: linear
---
%% ============================================================================
%% HYBRID DECISION FLOWCHART — Trợ Lý Duyệt Chi Phí Doanh Nghiệp
%% ============================================================================
%% Ý NGHĨA CÁC NHÁNH:
%%   moderation --refuse--> END           : Lớp 1 chặn prompt injection ngay cửa vào
%%   supervisor --direct--> direct_answer : 🤖 CHATBOT PATH (1 LLM call, 0 tool)
%%                                          Dùng cho câu hỏi kiến thức chung
%%   supervisor --react--> call_llm       : 🧠 REACT AGENT PATH (vòng lặp có tool)
%%                                          Dùng khi cần dữ liệu/hành động thật
%%   call_llm <-> tools                   : Vòng lặp Thought → Action → Observation
%%   tools --approval--> human_approval   : 🖐️ Chặn cứng trước khi chuyển tiền ≥ 10tr
%%   * --> exhausted                      : 🛑 Safe fallback khi chạm MAX_ITERATIONS
%% -
%% Xem thêm docs/flowchart.mermaid — bản vẽ tay đầy đủ, có tầng công cụ,
%% tầng dữ liệu, 4 lớp guardrails và ánh xạ sang rubric chấm điểm.
%% ============================================================================
graph TD;
	__start__([<p>__start__</p>]):::first
	moderation(moderation)
	supervisor(supervisor)
	direct_answer(direct_answer)
	call_llm(call_llm)
	tools(tools)
	human_approval(human_approval)
	exhausted(exhausted)
	__end__([<p>__end__</p>]):::last
	__start__ --> moderation;
	call_llm -. &nbsp;end&nbsp; .-> __end__;
	call_llm -.-> exhausted;
	call_llm -.-> tools;
	human_approval -. &nbsp;end&nbsp; .-> __end__;
	human_approval -.-> tools;
	moderation -. &nbsp;refuse&nbsp; .-> __end__;
	moderation -.-> supervisor;
	supervisor -. &nbsp;react&nbsp; .-> call_llm;
	supervisor -. &nbsp;direct&nbsp; .-> direct_answer;
	tools -. &nbsp;end&nbsp; .-> __end__;
	tools -. &nbsp;continue&nbsp; .-> call_llm;
	tools -.-> exhausted;
	tools -. &nbsp;approval&nbsp; .-> human_approval;
	direct_answer --> __end__;
	exhausted --> __end__;
	call_llm -. &nbsp;continue&nbsp; .-> call_llm;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
