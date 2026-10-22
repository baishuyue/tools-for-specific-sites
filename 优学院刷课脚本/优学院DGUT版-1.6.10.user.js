// ==UserScript==
// @name                      优学院DGUT版
// @version                   1.6.10
// @description               适配DGUT优学院（自动静音播放、自动做练习题、自动翻页、修改播放速率）
// @author                    baishuyue
// @match                     https://ua.dgut.edu.cn/learnCourse/learnCourse.html*
// @icon                      https://lms.dgut.edu.cn/ulearning/favicon.ico
// @grant                     GM_xmlhttpRequest
// @grant                     unsafeWindow
// @license                   MIT
// @namespace                 https://greasyfork.org/users/1540778
// @website                   https://soujiaoben.org/#/s?id=556678&host=greasyfork
// ==/UserScript==



(function () {
    'use strict';

    // build: 626169736875797565-2026
    const BUILD_REF = "YmFpc2h1eXll";

    /*  ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
     *  优学院自动静音播放、自动做练习题、自动翻页、修改播放速率脚本（适配东莞理工学院）
     *  重要提醒：使用风险自负，避免高倍速/长时间挂机，建议非核心课程使用，仅限个人学习使用，禁止商用
     *  基于作者Brush-JIM的脚本“优学院自动静音播放、自动做练习题、自动翻页、修改播放速率（改）”
     *  和作者 luluzzy. 的脚本“DGUT Ulearning Tool”（MIT协议）二次开发重构
     *  原脚本链接：https://greasyfork.org/zh-CN/scripts/555722-dgut-ulearning-tool
     *  ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
     */

    // 配置管理中心
    const AppConfig = {
        playbackRate: 1.5,
        autoPlay: true,
        autoMute: true,
        autoAdjustRate: true,
        autoFillAnswers: true,
        showAnswers: true,
        autoAnswerSingle: true,
        autoAnswerMulti: true,
        autoAnswerJudge: true,
        autoAnswerBlank: true,
        maxRetryCount: 7,
        getStorageKey: (key) => `ulearn_${key}`,
        load: function () {
            const prefix = 'ulearn_';
            Object.keys(this).forEach(key => {
                if (typeof this[key] !== 'function' && key !== 'maxRetryCount') {
                    const stored = localStorage.getItem(prefix + key);
                    if (stored !== null) {
                        if (typeof this[key] === 'boolean') {
                            this[key] = stored === 'true';
                        } else if (typeof this[key] === 'number') {
                            const value = Number(stored);
                            if (Number.isFinite(value)) this[key] = value;
                        } else {
                            this[key] = stored;
                        }
                    }
                }
            });
        },
        save: function () {
            const prefix = 'ulearn_';
            Object.keys(this).forEach(key => {
                if (typeof this[key] !== 'function' && key !== 'maxRetryCount') {
                    localStorage.setItem(prefix + key, this[key]);
                }
            });
        }
    };

    // 状态管理
    const AppState = {
        isPaused: false,
        isRestarting: false,
        isNavigating: false,
        answerInProgress: false,
        modalChecking: false,
        nextPageRetry: 0,
        reset: function () {
            this.nextPageRetry = 0;
            this.isNavigating = false;
        }
    };

    const Timing = {
        videoScanDelay: 3000,
        modalScanDelay: 2000,
        playbackLoopDelay: 750,
        mutationDebounceDelay: 800,
        simulatedActivityDelay: 10000,
        requestTimeout: 10000
    };

    const DomUtil = {
        isVisible: function (el) {
            return !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        },
        isPanelOnlyMutation: function (mutations) {
            const panel = document.querySelector('.ulearn-panel');
            if (!panel) return false;

            return mutations.every(mutation => {
                if (panel.contains(mutation.target)) return true;

                const changedNodes = [
                    ...Array.from(mutation.addedNodes || []),
                    ...Array.from(mutation.removedNodes || [])
                ];
                return changedNodes.length > 0 && changedNodes.every(node => panel.contains(node));
            });
        }
    };

    // 日志工具
    const Logger = {
        element: null,
        maxEntries: 120,
        init: function (el) {
            this.element = el;
        },
        log: function (message) {
            const timestamp = new Date().toLocaleTimeString();
            const logMsg = `[${timestamp}] ${message}`;
            console.log(`DGUT助手: ${logMsg}`);
            if (this.element) {
                const item = document.createElement('div');
                item.textContent = logMsg;
                this.element.appendChild(item);

                while (this.element.childElementCount > this.maxEntries) {
                    this.element.firstElementChild.remove();
                }

                this.element.scrollTop = this.element.scrollHeight;
            }
        }
    };

    // 答案处理服务
    class AnswerService {
        getQuestionType(questionEl) {
            const typeTag = questionEl.querySelector('.question-type-tag');
            if (!typeTag) return null;

            const typeText = typeTag.textContent.trim();
            if (typeText.includes('单选题')) return 'single';
            if (typeText.includes('多选题')) return 'multiple';
            if (typeText.includes('判断题')) return 'judge';
            if (typeText.includes('填空题')) return 'blank';
            return null;
        }

        processQuestion(questionId, answers) {
            if (AppState.isPaused || AppState.isRestarting) return;

            const questionEl = document.querySelector(`#question${questionId}`);
            if (!questionEl) {
                Logger.log(`未找到问题容器: ${questionId}`);
                return;
            }

            const type = this.getQuestionType(questionEl);
            if (!type) {
                Logger.log(`无法识别题型: ${questionId}`);
                return;
            }

            const enabledMap = {
                single: AppConfig.autoAnswerSingle,
                multiple: AppConfig.autoAnswerMulti,
                judge: AppConfig.autoAnswerJudge,
                blank: AppConfig.autoAnswerBlank
            };

            if (!enabledMap[type]) {
                Logger.log(`题目 ${questionId} 的${type}作答开关未开启，已跳过`);
                return;
            }

            const handlers = {
                single: () => this.handleChoice(questionEl, answers),
                multiple: () => this.handleChoice(questionEl, answers),
                judge: () => this.handleJudge(questionEl, answers),
                blank: () => this.handleBlank(questionEl, answers)
            };

            if (handlers[type]) {
                handlers[type]();
            } else {
                Logger.log(`不支持的题型: ${type}`);
            }
        }

        handleChoice(questionEl, answers) {
            const options = questionEl.querySelectorAll('.choice-item, .option-item, .question-option');
            if (!options.length) {
                Logger.log(`选择题选项未找到: ${questionEl.id}`);
                return;
            }

            const answerText = Array.isArray(answers) ? answers.join(',') : String(answers || '');

            options.forEach(option => {
                const optionLabel = option.querySelector('.option')?.textContent?.trim().replace('.', '') ||
                                   option.querySelector('.option-letter')?.textContent?.trim() ||
                                   option.querySelector('span:first-child')?.textContent?.trim().replace('.', '');

                if (optionLabel && answerText.includes(optionLabel)) {
                    const selector = option.querySelector('.checkbox, .option-checkbox, .radio');
                    if (selector && !selector.classList.contains('selected')) {
                        option.click();
                        if (!selector.classList.contains('selected')) {
                            selector.click();
                        }
                        Logger.log(`选中选项: ${optionLabel}`);
                    }
                }
            });
        }

        handleJudge(questionEl, answers) {
            const isCorrect = String(answers) === 'true';
            const btnSelector = isCorrect ? '.right-btn' : '.wrong-btn';
            const judgeBtn = questionEl.querySelector(btnSelector);

            if (judgeBtn && !judgeBtn.classList.contains('selected')) {
                judgeBtn.click();
                Logger.log(`判断题选择: ${isCorrect ? '正确' : '错误'}`);
            }
        }

        handleBlank(questionEl, answers) {
            const inputs = questionEl.querySelectorAll('textarea, .blank-input');
            const answerList = Array.isArray(answers) ? answers : [answers];
            answerList.forEach((ans, idx) => {
                if (inputs[idx]) {
                    const cleanedAns = this.cleanHtml(this.escapeHtml(String(ans || '')));
                    inputs[idx].value = cleanedAns;
                    $(inputs[idx]).trigger('change');
                }
            });
            Logger.log(`填空题已填充: ${answerList.join('; ')}`);
        }

        escapeHtml(str) {
            const entities = { 'lt': '<', 'gt': '>', 'nbsp': ' ', 'amp': '&', 'quot': '"' };
            return str.replace(/&(lt|gt|nbsp|amp|quot);/ig, (_, key) => entities[key]);
        }

        cleanHtml(str) {
            return str.replace(/(<[^>]+>|\\n|\\r)/g, ' ');
        }
    }

    // 视频控制服务
    class VideoController {
        constructor() {
            this.observer = null;
            this.processTimer = null;
            this.modalTimer = null;
            this.playbackTimer = null;
            this.boundEndedVideos = new WeakSet();
        }

        init() {
            this.setupVideoMonitoring();
        }

        setupVideoMonitoring() {
            if (this.observer) {
                this.observer.disconnect();
            }

            this.observer = new MutationObserver((mutations) => {
                if (AppState.isPaused || AppState.isRestarting || DomUtil.isPanelOnlyMutation(mutations)) return;

                this.queueProcessVideos(Timing.mutationDebounceDelay);
                this.queueModalCheck(Timing.mutationDebounceDelay);
            });
            this.observer.observe(document.body, { childList: true, subtree: true });
        }

        queueProcessVideos(delay = 0) {
            if (this.processTimer) clearTimeout(this.processTimer);
            this.processTimer = setTimeout(() => {
                this.processTimer = null;
                this.processVideos(true);
            }, delay);
        }

        queueModalCheck(delay = 0) {
            if (this.modalTimer) clearTimeout(this.modalTimer);
            this.modalTimer = setTimeout(() => {
                this.modalTimer = null;
                this.checkModals(true);
            }, delay);
        }

        stopPlaybackLoop() {
            if (this.playbackTimer) {
                clearTimeout(this.playbackTimer);
                this.playbackTimer = null;
            }
        }

        applyVideoSettings(video, logMute = false) {
            if (!video) return;

            if (AppConfig.autoAdjustRate && video.playbackRate !== AppConfig.playbackRate) {
                video.playbackRate = AppConfig.playbackRate;
            }

            if (AppConfig.autoMute && !video.muted) {
                video.muted = true;
                if (logMute) Logger.log("视频已静音");
            }
        }

        bindEndedHandler(video) {
            if (!video || this.boundEndedVideos.has(video)) return;

            video.addEventListener('ended', () => {
                Logger.log("视频播放完成");
                PageNavigator.goNext();
            }, { passive: true });

            this.boundEndedVideos.add(video);
        }

        processVideos(slept = false) {
            if (AppState.isPaused || AppState.isRestarting || !AppConfig.autoPlay) {
                this.stopPlaybackLoop();
                return;
            }

            if (AppState.answerInProgress) {
                this.queueProcessVideos(1000);
                return;
            }

            if (!slept) {
                this.queueProcessVideos(Timing.videoScanDelay);
                return;
            }

            const video = document.querySelector("video, mediaelementwrapper video:first-child");
            this.applyVideoSettings(video, true);

            const videoWrappers = Array.from(document.querySelectorAll('mediaelementwrapper video:first-child'));
            const statusIndicators = Array.from(document.querySelectorAll('.video-bottom span:first-child'));

            if (videoWrappers.length === 0) {
                if (!PageNavigator.isReady()) {
                    this.queueProcessVideos(1000);
                    return;
                }

                if (document.querySelector('.question-wrapper') && AppConfig.autoFillAnswers) {
                    AnswerProcessor.processQuiz();
                    return;
                }

                PageNavigator.goNext();
                return;
            }

            if (videoWrappers.length !== statusIndicators.length) {
                if (!PageNavigator.isReady()) {
                    this.queueProcessVideos(1000);
                    return;
                }

                PageNavigator.goNext();
                return;
            }

            const videoStates = videoWrappers.map((el, idx) => ({
                element: el,
                completed: statusIndicators[idx]?.getAttribute('data-bind') === 'text: $root.i18nMessageText().finished',
                currentTime: el.currentTime
            }));

            videoStates.forEach(state => {
                this.bindEndedHandler(state.element);
                this.applyVideoSettings(state.element);
            });

            this.controlPlayback(videoStates);
        }

        controlPlayback(videoStates) {
            this.stopPlaybackLoop();

            if (AppState.isPaused || AppState.isRestarting) return;

            const currentVideos = Array.from(document.querySelectorAll('mediaelementwrapper video:first-child'));
            if (videoStates.length !== currentVideos.length) {
                this.queueProcessVideos(500);
                return;
            }

            for (let i = 0; i < videoStates.length; i++) {
                if (videoStates[i].element !== currentVideos[i]) {
                    this.queueProcessVideos(500);
                    return;
                }

                this.applyVideoSettings(videoStates[i].element);
                if (videoStates[i].element.ended) videoStates[i].completed = true;

                if (!videoStates[i].completed) {
                    const targetVideo = (i > 0 && !videoStates[i - 1].completed) ? videoStates[i - 1] : videoStates[i];
                    const currentTime = targetVideo.element.currentTime;
                    const stalled = Math.abs((targetVideo.currentTime || 0) - currentTime) < 0.01;

                    if (targetVideo.element.paused || stalled) {
                        targetVideo.element.currentTime = Math.max(0, currentTime - 3);
                        targetVideo.element.play().catch(err => {
                            Logger.log(`播放失败: ${err.message}`);
                            AppRestarter.restart();
                        });
                    }

                    targetVideo.currentTime = targetVideo.element.currentTime;
                    this.applyVideoSettings(targetVideo.element);
                    this.playbackTimer = setTimeout(() => this.controlPlayback(videoStates), Timing.playbackLoopDelay);
                    return;
                }
            }

            PageNavigator.goNext();
        }

        checkModals(slept = false) {
            if (AppState.isPaused || AppState.isRestarting || AppState.answerInProgress) return;

            if (!slept) {
                this.queueModalCheck(Timing.modalScanDelay);
                return;
            }

            AppState.modalChecking = true;
            try {
                const questionPanel = document.querySelector('.question-wrapper');
                if (questionPanel && AppConfig.autoFillAnswers) {
                    AnswerProcessor.processQuiz();
                    return;
                }

                const statModal = document.getElementById('statModal');
                if (statModal && statModal.className.includes('in') && DomUtil.isVisible(statModal)) {
                    const nextStatButton = statModal.querySelector(
                        '.stat-next button[data-bind*="goNextPage"], .bottom-area button[data-bind*="goNextPage"]'
                    );
                    if (nextStatButton && DomUtil.isVisible(nextStatButton)) {
                        nextStatButton.click();
                        Logger.log("统计弹窗已点击下一页");
                    } else {
                        const buttons = Array.from(statModal.getElementsByTagName('button'));
                        const fallbackButton = buttons.find(btn => DomUtil.isVisible(btn) && /下一|继续|章节|Chapter|Next/i.test(btn.textContent || ''));
                        if (fallbackButton) fallbackButton.click();
                    }
                }

                const errorIndicator = $('.mobile-video-error');
                if (errorIndicator.length > 0 && errorIndicator.css('display') !== 'none') {
                    $('.try-again').click();
                    Logger.log("检测到视频错误，已尝试重试");
                }

                const alertModal = document.getElementById('alertModal');
                if (alertModal && alertModal.className.includes('in')) {
                    const operations = $('.modal-operation').children();
                    if (operations.length >= 2) {
                        operations[AppConfig.autoFillAnswers ? 0 : 1].click();
                    } else {
                        const continueBtn = $('.btn-submit');
                        continueBtn.each((_, btn) => {
                            if ($(btn).text() !== '提交') $(btn).click();
                        });
                    }
                    if (AppConfig.autoFillAnswers) AnswerProcessor.processQuiz();
                }
            } finally {
                AppState.modalChecking = false;
            }
        }
    }

    // 页面导航器
    const PageNavigator = {
        nextButtonSelector: [
            '.next-page-btn',
            '.mobile-next-page-btn',
            '#statModal.in .stat-next button[data-bind*="goNextPage"]',
            '#statModal.in .bottom-area button[data-bind*="goNextPage"]',
            '.stat-next button[data-bind*="goNextPage"]',
            '.bottom-area button[data-bind*="goNextPage"]',
            '.next-btn',
            '.btn-next',
            '.nextVideoBtn'
        ].join(', '),

        getPageVm: function () {
            const targetWindow = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;
            return targetWindow.koLearnCourseViewModel || window.koLearnCourseViewModel || null;
        },

        readObservable: function (value) {
            return typeof value === 'function' ? value() : value;
        },

        isNoMorePage: function (vm) {
            if (!vm) return false;
            const targetWindow = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;
            const nextName = this.readObservable(vm.nextPageName);
            const i18n = this.readObservable(vm.i18nMessageText) || targetWindow.i18nMessageText || window.i18nMessageText || {};
            return !!nextName && !!i18n.noMore && nextName === i18n.noMore;
        },

        isReady: function () {
            const vm = this.getPageVm();
            const currentPage = vm ? this.readObservable(vm.currentPage) : null;
            const currentPageLoaded = currentPage && typeof currentPage.isLoaded === 'function'
                ? !!currentPage.isLoaded()
                : !!currentPage;
            const hasPageControl = !!document.querySelector('.page-control-area, #statModal, .page-item .page-name.active');
            const loading = Array.from(document.querySelectorAll('.view-loader, .page-loader'))
                .some(el => DomUtil.isVisible(el));

            return !loading && (currentPageLoaded || hasPageControl);
        },

        tryGoNextByViewModel: function () {
            const vm = this.getPageVm();
            if (!vm || typeof vm.goNextPage !== 'function') return false;
            if (this.isNoMorePage(vm)) {
                this.finishNavigation('已到最后一页');
                return true;
            }

            try {
                vm.goNextPage.call(vm);
                Logger.log("已调用页面原生 goNextPage");
                return true;
            } catch (err) {
                Logger.log(`页面原生翻页失败，尝试按钮兜底: ${err.message}`);
                return false;
            }
        },

        findNextButton: function () {
            return Array.from(document.querySelectorAll(this.nextButtonSelector)).find(btn => {
                const text = (btn.textContent || '').trim();
                const disabled = btn.classList.contains('disabled') || btn.disabled || btn.getAttribute('aria-disabled') === 'true';
                const noMore = /没有更多|无更多|No more/i.test(text);

                return !disabled && !noMore && DomUtil.isVisible(btn);
            }) || null;
        },

        finishNavigation: function (message) {
            AppState.isPaused = true;
            const toggleBtn = document.getElementById('toggleScript');
            if (toggleBtn) {
                toggleBtn.innerText = '▶️ 继续运行';
                toggleBtn.className = 'control-btn resume-state';
            }
            Logger.log(`${message}，脚本已暂停`);
        },

        handleNextUnavailable: function (message) {
            if (!this.isReady()) {
                Logger.log(`${message}，页面仍在加载，延后重试`);
                videoController.queueProcessVideos(1000);
                return;
            }

            const vm = this.getPageVm();
            if (this.isNoMorePage(vm)) {
                this.finishNavigation('已到最后一页');
                return;
            }

            AppState.nextPageRetry++;
            Logger.log(`${message}（${AppState.nextPageRetry}/${AppConfig.maxRetryCount}）`);

            if (AppState.nextPageRetry >= AppConfig.maxRetryCount) {
                this.finishNavigation(`连续${AppConfig.maxRetryCount}次未找到可用下一页`);
            }
        },

        goNext: function () {
            if (AppState.isNavigating || AppState.isPaused || AppState.isRestarting ||
                AppState.answerInProgress || !AppConfig.autoPlay || AppState.modalChecking) {
                return;
            }

            Logger.log("尝试导航至下一页");
            if (this.tryGoNextByViewModel()) {
                AppState.nextPageRetry = 0;
                if (!AppState.isPaused) this.afterNavigation();
                return;
            }

            const nextButton = this.findNextButton();

            if (!nextButton) {
                this.handleNextUnavailable('未找到可用下一页按钮');
                return;
            }

            AppState.nextPageRetry = 0;
            AppState.isNavigating = true;
            Logger.log("锁定导航状态，防止重复操作");

            nextButton.click();
            Logger.log("已点击下一页按钮");

            this.afterNavigation();
        },

        afterNavigation: function () {
            AppState.isNavigating = true;
            setTimeout(() => {
                Logger.log("导航完成，解除锁定");
                AppState.isNavigating = false;

                setTimeout(() => {
                    if (!AppState.isPaused && !AppState.isRestarting) {
                        videoController.queueProcessVideos(0);
                        videoController.queueModalCheck(0);
                    }
                }, 1000);
            }, 3000);
        }
    };

    // 答案处理器
    const AnswerProcessor = {
        _authCookie: '',
        _authCache: '',

        processQuiz: function () {
            if (AppState.isPaused || AppState.isRestarting || AppState.answerInProgress || !AppConfig.autoFillAnswers) {
                return;
            }

            AppState.answerInProgress = true;
            Logger.log("检测到测验页面，开始处理答案");

            let questionIds = [];
            let waitTimeout = null;

            const waitForQuestions = setInterval(() => {
                const currentPanels = $('.question-wrapper');
                if (currentPanels.length > 0) {
                    clearInterval(waitForQuestions);
                    if (waitTimeout) clearTimeout(waitTimeout);

                    currentPanels.each((_, panel) => {
                        const id = $(panel).attr('id');
                        if (id && id.startsWith('question')) {
                            questionIds.push(id.replace('question', ''));
                        } else {
                            Logger.log("发现无效问题ID，已跳过");
                        }
                    });

                    questionIds = [...new Set(questionIds)];
                    Logger.log(`共检测到 ${questionIds.length} 道题目`);

                    let pageId = '';
                    let found = false;
                    const pageItems = $('.page-item');
                    pageItems.each((_, item) => {
                        if (found) return;
                        const pageName = $(item).find('.page-name');
                        if (pageName.length > 0 && pageName[0].className.includes('active')) {
                            const idAttr = $(item).attr('id');
                            pageId = idAttr.slice(idAttr.search(/\d/g));
                            found = true;
                        }
                    });

                    if (!found) {
                        AppState.answerInProgress = false;
                        PageNavigator.goNext();
                        return;
                    }

                    if (questionIds.length === 0) {
                        Logger.log("未发现有效题目，跳转至下一页");
                        AppState.answerInProgress = false;
                        PageNavigator.goNext();
                        return;
                    }

                    const answerService = new AnswerService();
                    const total = questionIds.length;
                    let processed = 0;

                    const processNext = (index) => {
                        if (index >= total) {
                            Logger.log(`所有 ${total} 道题目处理完毕`);
                            setTimeout(() => {
                                if (AppConfig.autoPlay) {
                                    $('textarea, .blank-input').trigger('change');
                                    const submitBtn = $('.btn-submit');
                                    if (submitBtn.length > 0) {
                                        submitBtn.click();
                                        Logger.log("已提交答案");
                                    }

                                    const videos = $('video').filter((_, v) => v.src !== "");
                                    if (videos.length === 0) {
                                        AppState.answerInProgress = false;
                                        PageNavigator.goNext();
                                        return;
                                    }
                                }
                                AppState.answerInProgress = false;
                            }, 1000);
                            return;
                        }

                        const qId = questionIds[index];
                        Logger.log(`处理第 ${index + 1}/${total} 题 (ID: ${qId})`);
                        this.fetchAnswer(qId, pageId, answerService, () => {
                            processed++;
                            Logger.log(`第 ${index + 1} 题处理完成 (${processed}/${total})`);
                            processNext(index + 1);
                        });
                    };

                    processNext(0);
                }
            }, 500);

            waitTimeout = setTimeout(() => {
                clearInterval(waitForQuestions);
                if (questionIds.length === 0) {
                    Logger.log("超时未检测到题目，跳转至下一页");
                    AppState.answerInProgress = false;
                    PageNavigator.goNext();
                }
            }, 5000);
        },

        fetchAnswer: function (questionId, parentId, answerService, callback) {
            let finished = false;
            const finish = () => {
                if (finished) return;
                finished = true;
                callback();
            };

            if (AppState.isPaused || AppState.isRestarting) {
                finish();
                return;
            }

            const auth = this.getAuthorization();
            if (!auth) {
                Logger.log("获取认证信息失败，无法请求答案");
                finish();
                return;
            }

            GM_xmlhttpRequest({
                method: "GET",
                url: `https://ua.dgut.edu.cn/uaapi/questionAnswer/${questionId}?parentId=${parentId}`,
                headers: {
                    "UA-AUTHORIZATION": auth,
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": window.location.href
                },
                timeout: Timing.requestTimeout,
                onload: (res) => {
                    try {
                        const data = JSON.parse(res.responseText);
                        const answers = data.correctAnswerList || data.answer || [];
                        const hasAnswers = Array.isArray(answers)
                            ? answers.length > 0
                            : answers !== undefined && answers !== null && String(answers) !== '';

                        if (AppConfig.showAnswers && hasAnswers) {
                            Logger.log(`题目 ${questionId} 答案: ${answers}`);
                        }

                        if (hasAnswers) {
                            answerService.processQuestion(questionId, answers);
                        } else {
                            Logger.log(`题目 ${questionId} 未找到答案`);
                        }
                    } catch (err) {
                        Logger.log(`解析答案失败: ${err.message}`);
                        console.error("答案解析错误:", err);
                    } finally {
                        finish();
                    }
                },
                onerror: (err) => {
                    Logger.log(`请求答案失败: ${(err && err.message) || '网络异常'}`);
                    console.error("答案请求错误:", err);
                    finish();
                },
                ontimeout: () => {
                    Logger.log(`题目 ${questionId} 请求超时，已跳过`);
                    finish();
                }
            });
        },

        getAuthorization: function () {
            const cookie = document.cookie;
            if (cookie === this._authCookie) return this._authCache;

            this._authCookie = cookie;
            this._authCache = cookie.split(";")
                .map(c => c.trim().split("="))
                .find(([k]) => k === "AUTHORIZATION")?.[1] || "";
            return this._authCache;
        }
    };

    // 应用重启器
    const AppRestarter = {
        restart: function () {
            if (AppState.isRestarting) return;

            AppState.isRestarting = true;
            Logger.log("检测到异常，尝试重启服务...");

            const toggleBtn = document.getElementById('toggleScript');
            if (!toggleBtn) {
                AppState.isRestarting = false;
                Logger.log("未找到控制按钮，已取消重启");
                return;
            }

            if (!AppState.isPaused) {
                toggleBtn.click();
            }

            setTimeout(() => {
                Logger.log("重启中，恢复服务...");
                toggleBtn.click();

                setTimeout(() => {
                    AppState.isRestarting = false;
                    Logger.log("服务重启完成");
                }, 1000);
            }, 2000);
        }
    };

    // UI组件
    class UIController {
        constructor() {
            this.panel = null;
        }

        render() {
            this.loadStyles();
            this.createPanel();
            this.bindEvents();
            AppConfig.load();
            this.syncConfigToUI();
        }

        loadStyles() {
            if (document.getElementById('ulearn-helper-style')) return;

            const style = document.createElement('style');
            style.id = 'ulearn-helper-style';
            style.textContent = `
                .ulearn-panel {position:fixed;top:100px;right:30px;z-index:999999;background:rgba(0,0,0,0.7);color:#fff;padding:10px 12px;border-radius:8px;font-size:14px;width:320px;cursor:move}
                .ulearn-panel:hover {opacity:0.95}
                .drag-handle {cursor:move;padding:5px;text-align:center;background:rgba(255,255,255,0.1);border-radius:4px;margin-bottom:8px}
                .panel-title {text-align:center;margin:5px 0;font-weight:bold}
                .control-btn {margin:4px;padding:3px 8px;cursor:pointer;border:none;border-radius:3px;background:rgba(255,255,255,0.2);color:white}
                .control-btn:hover {background:rgba(255,255,255,0.3)}
                .log-container {height:150px;overflow:auto;background:#111;padding:4px;border-radius:4px;font-size:12px;line-height:1.5;margin-top:8px}
                .setting-group {padding-left:15px;margin:5px 0}
                .setting-item {position:relative;list-style:none;margin:5px 0}
                .setting-input {position:absolute;right:5px}
                .section-title {font-weight:bold;margin-top:10px;margin-bottom:5px;padding-bottom:3px;border-bottom:1px solid rgba(255,255,255,0.2)}
                .pause-state {background:rgba(231, 76, 60, 0.5)!important;margin-top:5px;}
                .resume-state {background:rgba(46, 204, 113, 0.5)!important;margin-top:5px;}
            `;
            document.head.appendChild(style);
        }

        createPanel() {
            const existingPanel = document.querySelector('.ulearn-panel');
            if (existingPanel) {
                if (existingPanel.querySelector('#autoAnswerMulti')) {
                    this.panel = existingPanel;
                    Logger.init(document.getElementById('logContainer'));
                    return;
                }

                existingPanel.remove();
            }

            const panel = document.createElement('div');
            panel.className = 'ulearn-panel';
            panel.innerHTML = `
                <div class="drag-handle">拖动调整位置</div>
                <div class="panel-title">DGUT 优学院助手</div>

                <button id="toggleScript" class="control-btn pause-state" style="width:100%;">⏸️ 暂停脚本</button>

                <div class="section-title">视频控制</div>
                <div>播放倍速: <span id="rateDisplay">${AppConfig.playbackRate}</span>x（1-15x）</div>
                <button id="speedUp" class="control-btn">➕ 加速</button>
                <button id="speedDown" class="control-btn">➖ 减速</button>

                <div class="section-title">视频设置</div>
                <p style="color:#ff9999;font-size:12px;">提示：播放失败时将自动重启</p>
                <ul class="setting-group">
                    <li class="setting-item">自动播放/翻页?<input class="setting-input" id="autoPlay" type="checkbox" checked></li>
                    <li class="setting-item">自动静音?<input class="setting-input" id="autoMute" type="checkbox" checked></li>
                    <li class="setting-item">自动调整速率?<input class="setting-input" id="autoAdjustRate" type="checkbox" checked></li>
                    <li class="setting-item">播放速率<input class="setting-input" id="rateInput" type="number" value="${AppConfig.playbackRate}" step="1" min="1" max="15"></li>
                </ul>

                <div class="section-title">自动作答</div>
                <ul class="setting-group">
                    <li class="setting-item">自动作答(总开关)?<input class="setting-input" id="autoFillAnswers" type="checkbox" checked></li>
                    <li class="setting-item">显示答案?<input class="setting-input" id="showAnswers" type="checkbox" checked></li>
                    <li class="setting-item">自动答选择题?<input class="setting-input" id="autoAnswerSingle" type="checkbox" checked></li>
                    <li class="setting-item">自动答多选题?<input class="setting-input" id="autoAnswerMulti" type="checkbox" checked></li>
                    <li class="setting-item">自动答判断题?<input class="setting-input" id="autoAnswerJudge" type="checkbox" checked></li>
                    <li class="setting-item">自动答填空题?<input class="setting-input" id="autoAnswerBlank" type="checkbox" checked></li>
                </ul>

                <button id="saveSettings" class="control-btn" style="width:100%;margin-top:10px;">保存设置</button>

                <div style="margin-top:8px;">操作日志:</div>
                <div id="logContainer" class="log-container"></div>
            `;
            document.body.appendChild(panel);
            this.panel = panel;
            Logger.init(document.getElementById('logContainer'));
        }

        bindEvents() {
            if (this.panel.dataset.bound === 'true') return;
            this.panel.dataset.bound = 'true';

            // 拖动功能
            const dragHandle = this.panel.querySelector('.drag-handle');
            if (dragHandle) {
                dragHandle.addEventListener('mousedown', (e) => {
                    e.preventDefault();
                    const offsetX = e.clientX - this.panel.offsetLeft;
                    const offsetY = e.clientY - this.panel.offsetTop;

                    const moveHandler = (e) => {
                        this.panel.style.left = `${e.clientX - offsetX}px`;
                        this.panel.style.top = `${e.clientY - offsetY}px`;
                        this.panel.style.right = 'auto';
                    };

                    document.addEventListener('mousemove', moveHandler);
                    document.addEventListener('mouseup', () => {
                        document.removeEventListener('mousemove', moveHandler);
                    }, { once: true });
                });
            }

            // 速率控制
            document.getElementById('speedUp').addEventListener('click', () => {
                if (AppState.isRestarting) return;
                AppConfig.playbackRate = Math.min(15, (Number(AppConfig.playbackRate) || 1) + 1);
                this.updateRateDisplay();
            });

            document.getElementById('speedDown').addEventListener('click', () => {
                if (AppState.isRestarting) return;
                AppConfig.playbackRate = Math.max(1, (Number(AppConfig.playbackRate) || 1) - 1);
                this.updateRateDisplay();
            });

            // 速率输入框
            const rateInput = document.getElementById('rateInput');
            rateInput.addEventListener('change', () => {
                if (AppState.isRestarting) return;
                let val = parseFloat(rateInput.value);
                if (!Number.isFinite(val)) val = 1;
                val = Math.max(1, Math.min(15, val));
                rateInput.value = val;
                AppConfig.playbackRate = val;
                this.updateRateDisplay();
            });

            // 暂停/继续按钮
            const toggleBtn = document.getElementById('toggleScript');
            toggleBtn.addEventListener('click', () => {
                if (AppState.isRestarting) {
                    Logger.log("重启中，暂不支持操作");
                    return;
                }

                AppState.isPaused = !AppState.isPaused;
                if (AppState.isPaused) {
                    toggleBtn.innerText = '▶️ 继续运行';
                    toggleBtn.className = 'control-btn resume-state';
                    Logger.log("脚本已暂停");
                } else {
                    toggleBtn.innerText = '⏸️ 暂停脚本';
                    toggleBtn.className = 'control-btn pause-state';
                    Logger.log("脚本已恢复运行");
                    videoController.queueProcessVideos(0);
                    videoController.queueModalCheck(0);
                }
            });

            // 自动播放开关联动
            const autoPlayToggle = document.getElementById('autoPlay');
            autoPlayToggle.addEventListener('change', () => {
                if (AppState.isRestarting) return;
                const autoMute = document.getElementById('autoMute');
                const autoAdjust = document.getElementById('autoAdjustRate');
                if (!autoPlayToggle.checked) {
                    autoMute.checked = false;
                    autoAdjust.checked = false;
                } else {
                    autoMute.checked = true;
                    autoAdjust.checked = true;
                }
            });

            // 自动作答总开关联动
            const autoAnswerToggle = document.getElementById('autoFillAnswers');
            autoAnswerToggle.addEventListener('change', () => {
                if (AppState.isRestarting) return;
                const showAns = document.getElementById('showAnswers');
                const single = document.getElementById('autoAnswerSingle');
                const multi = document.getElementById('autoAnswerMulti');
                const judge = document.getElementById('autoAnswerJudge');
                const blank = document.getElementById('autoAnswerBlank');

                if (!autoAnswerToggle.checked) {
                    showAns.checked = false;
                    single.checked = false;
                    multi.checked = false;
                    judge.checked = false;
                    blank.checked = false;
                } else {
                    showAns.checked = true;
                    single.checked = true;
                    multi.checked = true;
                    judge.checked = true;
                    blank.checked = true;
                }
            });

            // 保存设置
            document.getElementById('saveSettings').addEventListener('click', () => {
                if (AppState.isRestarting) {
                    Logger.log("重启中，暂不支持保存");
                    return;
                }

                AppConfig.autoPlay = document.getElementById('autoPlay').checked;
                AppConfig.autoMute = document.getElementById('autoMute').checked;
                AppConfig.autoAdjustRate = document.getElementById('autoAdjustRate').checked;
                AppConfig.autoFillAnswers = document.getElementById('autoFillAnswers').checked;
                AppConfig.showAnswers = document.getElementById('showAnswers').checked;
                AppConfig.autoAnswerSingle = document.getElementById('autoAnswerSingle').checked;
                AppConfig.autoAnswerMulti = document.getElementById('autoAnswerMulti').checked;
                AppConfig.autoAnswerJudge = document.getElementById('autoAnswerJudge').checked;
                AppConfig.autoAnswerBlank = document.getElementById('autoAnswerBlank').checked;
                const rateValue = parseFloat(document.getElementById('rateInput').value);
                AppConfig.playbackRate = Number.isFinite(rateValue) ? Math.max(1, Math.min(15, rateValue)) : 1;

                AppConfig.save();
                localStorage.setItem(AppConfig.getStorageKey('paused'), AppState.isPaused);
                localStorage.setItem(AppConfig.getStorageKey('retryCount'), AppState.nextPageRetry);

                this.updateRateDisplay();
                Logger.log("设置已保存");

                if (!AppState.isPaused) {
                    videoController.queueProcessVideos(0);
                    videoController.queueModalCheck(0);
                }
            });
        }

        syncConfigToUI() {
            document.getElementById('autoPlay').checked = AppConfig.autoPlay;
            document.getElementById('autoMute').checked = AppConfig.autoMute;
            document.getElementById('autoAdjustRate').checked = AppConfig.autoAdjustRate;
            document.getElementById('autoFillAnswers').checked = AppConfig.autoFillAnswers;
            document.getElementById('showAnswers').checked = AppConfig.showAnswers;
            document.getElementById('autoAnswerSingle').checked = AppConfig.autoAnswerSingle;
            document.getElementById('autoAnswerMulti').checked = AppConfig.autoAnswerMulti;
            document.getElementById('autoAnswerJudge').checked = AppConfig.autoAnswerJudge;
            document.getElementById('autoAnswerBlank').checked = AppConfig.autoAnswerBlank;
            document.getElementById('rateInput').value = AppConfig.playbackRate;

            AppState.isPaused = localStorage.getItem(AppConfig.getStorageKey('paused')) === 'true';
            AppState.nextPageRetry = parseInt(localStorage.getItem(AppConfig.getStorageKey('retryCount')) || '0');

            const toggleBtn = document.getElementById('toggleScript');
            if (AppState.isPaused) {
                toggleBtn.innerText = '▶️ 继续运行';
                toggleBtn.className = 'control-btn resume-state';
            } else {
                toggleBtn.innerText = '⏸️ 暂停脚本';
                toggleBtn.className = 'control-btn pause-state';
            }
        }

        updateRateDisplay() {
            const display = document.getElementById('rateDisplay');
            const input = document.getElementById('rateInput');
            display.innerText = AppConfig.playbackRate;
            input.value = AppConfig.playbackRate;

            document.querySelectorAll('video').forEach(video => {
                video.playbackRate = AppConfig.playbackRate;
            });

            Logger.log(`播放速率已调整为 ${AppConfig.playbackRate}x`);
        }
    }

    // 防检测处理
    function setupAntiDetection() {
        const targetWindow = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;

        try {
            // 模拟用户代理
            Object.defineProperty(targetWindow.navigator, "userAgent", {
                get: () => "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/83.0.4103.61 Safari/537.36",
                configurable: true
            });
        } catch (err) {
            Logger.log(`用户代理模拟未生效: ${err.message}`);
        }

        // 模拟鼠标活动
        setInterval(() => {
            if (!AppState.isPaused && !AppState.isRestarting) {
                targetWindow.document.dispatchEvent(new MouseEvent('mousemove', {
                    bubbles: true,
                    cancelable: true,
                    clientX: 1,
                    clientY: 1
                }));
            }
        }, Timing.simulatedActivityDelay);
    }

    // 初始化应用
    const uiController = new UIController();
    const videoController = new VideoController();

    function initApp() {
        uiController.render();
        videoController.init();
        setupAntiDetection();

        if (!AppState.isPaused && !AppState.isRestarting) {
            videoController.queueProcessVideos(0);
            videoController.queueModalCheck(0);
        }
    }

    setTimeout(initApp, 3000);
})();
