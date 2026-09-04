    const ALERT_HIDE_MS = 2000;
    const alertAutoHideScheduled = new WeakSet();
    const NAV_EXACT_PATHS = new Set([
      '/admin', '/catalog', '/cart', '/pending', '/rejected', '/profile', '/support',
    ]);

    function formatLocalDatetimes(root) {
      const scope = root || document;
      const dateFmt = new Intl.DateTimeFormat('ru-RU', {
        day: 'numeric', month: 'short', year: 'numeric',
      });
      const dateTimeFmt = new Intl.DateTimeFormat('ru-RU', {
        day: 'numeric', month: 'short', year: 'numeric',
        hour: '2-digit', minute: '2-digit',
      });
      scope.querySelectorAll('time.local-datetime').forEach((el) => {
        const raw = el.getAttribute('datetime');
        if (!raw) return;
        const date = new Date(raw);
        if (Number.isNaN(date.getTime())) return;
        const fmt = el.classList.contains('local-date') ? dateFmt : dateTimeFmt;
        el.textContent = fmt.format(date).replace(/,\s*/g, ' ');
        el.setAttribute('title', date.toLocaleString('ru-RU'));
      });
    }

    function shouldAutoHideAlert(el) {
      if (el.closest('[data-credential-flash]') || el.hasAttribute('data-credential-flash')) {
        return false;
      }
      if (el.hasAttribute('data-alert-persist')) {
        return false;
      }
      if (el.classList.contains('alert-danger')) {
        return false;
      }
      return el.classList.contains('alert-success') || el.classList.contains('alert-warning') || el.classList.contains('alert-info');
    }

    function scheduleAutoHide(el) {
      if (alertAutoHideScheduled.has(el)) {
        return;
      }
      alertAutoHideScheduled.add(el);
      const hideMs = Number.parseInt(el.dataset.alertHideMs, 10) || ALERT_HIDE_MS;
      window.setTimeout(() => {
        el.style.transition = 'opacity 0.3s ease';
        el.style.opacity = '0';
        window.setTimeout(() => el.remove(), 300);
      }, hideMs);
    }

    function initAlertAutoHide(root) {
      const scope = root || document;
      scope.querySelectorAll('.alert').forEach((el) => {
        if (!shouldAutoHideAlert(el)) {
          return;
        }
        scheduleAutoHide(el);
      });
      scope.querySelectorAll('.settings-toast').forEach(scheduleAutoHide);
    }

    function updateSidebarActive() {
      const path = window.location.pathname;
      document.querySelectorAll('.sidebar nav a[href]').forEach((link) => {
        const href = link.getAttribute('href');
        if (!href || href === '/logout') {
          return;
        }
        let active;
        if (href === '/profile') {
          active = path === '/profile' || path.startsWith('/change-password');
        } else if (NAV_EXACT_PATHS.has(href)) {
          active = path === href;
        } else {
          active = path === href || path.startsWith(href + '/');
        }
        link.classList.toggle('active', active);
      });
    }

    function initDescriptionToggles(root) {
      const scope = root || document;
      scope.querySelectorAll('[data-description-toggle]:not([data-description-init])').forEach((rootEl) => {
        const text = rootEl.querySelector('.product-description-text');
        const button = rootEl.querySelector('.product-description-toggle');
        if (!text || !button) {
          return;
        }
        rootEl.setAttribute('data-description-init', '');

        const syncToggleVisibility = () => {
          if (text.classList.contains('is-expanded')) {
            return;
          }
          text.classList.add('is-clamped');
          const needsToggle = text.scrollHeight > text.clientHeight + 1;
          if (!needsToggle) {
            text.classList.remove('is-clamped');
            button.hidden = true;
            button.setAttribute('aria-expanded', 'false');
            return;
          }
          button.hidden = false;
          button.textContent = 'Показать полностью';
          button.setAttribute('aria-expanded', 'false');
        };

        button.addEventListener('click', () => {
          const expanded = text.classList.toggle('is-expanded');
          text.classList.toggle('is-clamped', !expanded);
          button.textContent = expanded ? 'Скрыть' : 'Показать полностью';
          button.setAttribute('aria-expanded', expanded ? 'true' : 'false');
          button.focus();
        });

        syncToggleVisibility();
        window.addEventListener('resize', syncToggleVisibility);
      });
    }

    function initProductGalleries(root) {
      const scope = root || document;

      scope.querySelectorAll('[data-catalog-card-gallery]:not([data-gallery-init])').forEach((rootEl) => {
        const viewport = rootEl.querySelector('.catalog-card-gallery__viewport');
        const images = [...rootEl.querySelectorAll('.catalog-card-gallery__viewport img')];
        const zones = [...rootEl.querySelectorAll('.catalog-card-gallery__zone')];
        const dots = [...rootEl.querySelectorAll('.catalog-card-gallery__dot')];
        if (!viewport || images.length <= 1) return;
        rootEl.setAttribute('data-gallery-init', '');
        const show = (index) => {
          images.forEach((img, i) => img.classList.toggle('is-active', i === index));
          dots.forEach((dot, i) => dot.classList.toggle('is-active', i === index));
        };
        zones.forEach((zone) => {
          zone.addEventListener('mouseenter', () => show(Number(zone.dataset.index || 0)));
        });
        rootEl.addEventListener('mouseleave', () => show(0));
        viewport.addEventListener('scroll', () => {
          const index = Math.round(viewport.scrollLeft / Math.max(viewport.clientWidth, 1));
          dots.forEach((dot, i) => dot.classList.toggle('is-active', i === index));
        }, { passive: true });
      });

      scope.querySelectorAll('[data-product-gallery]:not([data-gallery-init])').forEach((rootEl) => {
        const main = rootEl.querySelector('.gallery-main-image');
        const counter = rootEl.querySelector('.gallery-counter');
        const thumbs = [...rootEl.querySelectorAll('.gallery-thumb')];
        if (!main) {
          return;
        }
        rootEl.setAttribute('data-gallery-init', '');
        const sources = thumbs.length
          ? thumbs.map((thumb) => thumb.dataset.src)
          : [main.getAttribute('src')].filter(Boolean);
        let index = 0;

        const show = (next) => {
          if (!sources.length) {
            return;
          }
          index = (next + sources.length) % sources.length;
          main.src = sources[index];
          if (counter) {
            counter.textContent = `${index + 1} / ${sources.length}`;
          }
          thumbs.forEach((thumb, i) => thumb.classList.toggle('active', i === index));
          const dialog = document.getElementById('gallery-lightbox');
          if (dialog && dialog.open) {
            const image = dialog.querySelector('.gallery-lightbox__image');
            const lightboxCounter = dialog.querySelector('.gallery-lightbox__counter');
            if (image) {
              image.src = sources[index];
              image.alt = main.alt || '';
            }
            if (lightboxCounter) {
              lightboxCounter.textContent = `${index + 1} / ${sources.length}`;
            }
          }
        };

        rootEl.querySelector('.gallery-prev')?.addEventListener('click', () => show(index - 1));
        rootEl.querySelector('.gallery-next')?.addEventListener('click', () => show(index + 1));
        thumbs.forEach((thumb, i) => thumb.addEventListener('click', () => show(i)));

        const lightboxTrigger = rootEl.querySelector('[data-gallery-lightbox-trigger]');
        if (lightboxTrigger) {
          const ensureLightbox = () => {
            let dialog = document.getElementById('gallery-lightbox');
            if (dialog) {
              return dialog;
            }
            dialog = document.createElement('dialog');
            dialog.id = 'gallery-lightbox';
            dialog.className = 'gallery-lightbox';
            dialog.innerHTML = '<button type="button" class="gallery-lightbox__close" aria-label="Закрыть"><i data-lucide="x"></i></button><button type="button" class="gallery-lightbox__prev" aria-label="Предыдущее фото"><i data-lucide="chevron-left"></i></button><img alt="" class="gallery-lightbox__image"><button type="button" class="gallery-lightbox__next" aria-label="Следующее фото"><i data-lucide="chevron-right"></i></button><span class="gallery-lightbox__counter"></span>';
            document.body.appendChild(dialog);
            const step = (delta) => dialog._step && dialog._step(delta);
            dialog.querySelector('.gallery-lightbox__close')?.addEventListener('click', () => dialog.close());
            dialog.querySelector('.gallery-lightbox__prev')?.addEventListener('click', (event) => {
              event.stopPropagation();
              step(-1);
            });
            dialog.querySelector('.gallery-lightbox__next')?.addEventListener('click', (event) => {
              event.stopPropagation();
              step(1);
            });
            dialog.addEventListener('click', (event) => {
              if (event.target === dialog) {
                dialog.close();
              }
            });
            dialog.addEventListener('keydown', (event) => {
              if (event.key === 'ArrowLeft') {
                event.preventDefault();
                step(-1);
              } else if (event.key === 'ArrowRight') {
                event.preventDefault();
                step(1);
              }
            });
            return dialog;
          };
          const openLightbox = () => {
            const dialog = ensureLightbox();
            dialog._step = (delta) => {
              if (sources.length > 1) {
                show(index + delta);
              }
            };
            dialog.classList.toggle('is-single', sources.length <= 1);
            const image = dialog.querySelector('.gallery-lightbox__image');
            const lightboxCounter = dialog.querySelector('.gallery-lightbox__counter');
            if (image) {
              image.src = sources[index] || main.currentSrc || main.src;
              image.alt = main.alt || '';
            }
            if (lightboxCounter) {
              lightboxCounter.textContent = `${index + 1} / ${sources.length}`;
            }
            if (typeof dialog.showModal === 'function') {
              dialog.showModal();
              lucide.createIcons();
            }
          };
          lightboxTrigger.style.cursor = 'zoom-in';
          lightboxTrigger.addEventListener('click', openLightbox);
          lightboxTrigger.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              openLightbox();
            }
          });
        }
      });
    }
    function initFilterBars(root) {
      const scope = root || document;
      scope.querySelectorAll('[data-filter-bar]:not([data-filter-bar-init])').forEach((form) => {
        form.setAttribute('data-filter-bar-init', '');
        const dialog = form.querySelector('.filter-bar__dialog');
        const mobileMq = window.matchMedia(form.dataset.filterMobileMq || '(max-width: 700px)');

        const syncTwinFields = () => {
          const isMobile = mobileMq.matches;
          form.querySelectorAll('[data-filter-field-sync]').forEach((primary) => {
            const key = primary.dataset.filterFieldSync;
            const twin = form.querySelector(`[data-filter-field-twin="${key}"]`);
            if (!twin) {
              return;
            }
            if (isMobile) {
              twin.value = primary.value;
              primary.disabled = true;
            } else {
              primary.disabled = false;
              primary.value = twin.value;
              twin.disabled = true;
            }
          });
        };

        const prepareSubmit = () => {
          form.querySelectorAll('[data-filter-field-sync]').forEach((primary) => {
            const key = primary.dataset.filterFieldSync;
            const twin = form.querySelector(`[data-filter-field-twin="${key}"]`);
            if (!twin || !mobileMq.matches) {
              return;
            }
            primary.value = twin.value;
            primary.disabled = false;
          });
        };

        form.addEventListener('submit', prepareSubmit);

        form.querySelector('[data-filter-dialog-open]')?.addEventListener('click', () => {
          syncTwinFields();
          dialog?.showModal();
        });
        form.querySelectorAll('[data-filter-dialog-close]').forEach((button) => {
          button.addEventListener('click', () => dialog?.close());
        });
        dialog?.addEventListener('click', (event) => {
          if (event.target === dialog) {
            dialog.close();
          }
        });
        dialog?.addEventListener('close', syncTwinFields);

        const clearUrl = form.dataset.filterClearUrl;
        const searchInput = form.querySelector('input[name="q"]');
        if (searchInput && clearUrl) {
          let prev = searchInput.value;
          searchInput.addEventListener('search', () => {
            if (!searchInput.value.trim()) {
              window.location.href = clearUrl;
            }
          });
          searchInput.addEventListener('input', () => {
            const value = searchInput.value;
            if (prev.trim() && !value.trim()) {
              window.location.href = clearUrl;
            }
            prev = value;
          });
        }

        form.querySelectorAll('[data-filter-auto-submit]').forEach((control) => {
          control.addEventListener('change', () => {
            if (!control.disabled) {
              form.requestSubmit();
            }
          });
        });

        mobileMq.addEventListener('change', syncTwinFields);
        syncTwinFields();
      });
    }


    function initAuthPages(root) {
      const scope = root || document;
      scope.querySelectorAll('[data-auth-page]:not([data-auth-page-init])').forEach((page) => {
        page.setAttribute('data-auth-page-init', '');
        const backdrops = page.querySelectorAll('.auth-page__backdrop');
        const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        const finePointer = window.matchMedia('(pointer: fine)').matches;
        if (!reducedMotion && finePointer) {
          const spotSize = parseFloat(getComputedStyle(page).getPropertyValue('--auth-spot')) || 140;
          const spotPad = spotSize / 2;
          const paintSpotlight = (x, y) => {
            const px = `${x}px`;
            const py = `${y}px`;
            page.style.setProperty('--auth-px', px);
            page.style.setProperty('--auth-py', py);
            backdrops.forEach((layer) => {
              layer.style.setProperty('--auth-px', px);
              layer.style.setProperty('--auth-py', py);
            });
          };
          const onPointerMove = (event) => {
            const maxX = window.innerWidth - spotPad;
            const maxY = window.innerHeight - spotPad;
            const x = Math.min(maxX, Math.max(spotPad, event.clientX));
            const y = Math.min(maxY, Math.max(spotPad, event.clientY));
            paintSpotlight(x, y);
          };
          document.addEventListener('pointermove', onPointerMove, { capture: true, passive: true });
          document.addEventListener('mousemove', onPointerMove, { capture: true, passive: true });
        }
        page.querySelectorAll('[data-auth-form]').forEach((form) => {
          form.addEventListener('submit', () => {
            form.setAttribute('aria-busy', 'true');
            const submit = form.querySelector('[type="submit"]');
            if (!submit) {
              return;
            }
            submit.disabled = true;
            submit.classList.add('is-loading');
            submit.setAttribute('aria-busy', 'true');
          });
        });
      });
    }

    function initLucideIcons(root) {
      if (typeof lucide === 'undefined') {
        return;
      }
      const scope = root || document;
      lucide.createIcons({
        root: scope === document ? document : scope,
      });
    }




    function initInvoiceItemForms(root) {
      const scope = root || document;
      scope.querySelectorAll('.invoice-items-edit-form').forEach((form) => {
        if (form.dataset.invoiceRemoveInit === '1') {
          return;
        }
        form.dataset.invoiceRemoveInit = '1';
        form.addEventListener('click', (event) => {
          const stepBtn = event.target.closest('.invoice-qty-step');
          if (stepBtn && form.contains(stepBtn)) {
            const stepper = stepBtn.closest('.invoice-qty-stepper');
            const input = stepper ? stepper.querySelector('.invoice-qty-input') : null;
            if (!input) {
              return;
            }
            const step = Number(stepBtn.dataset.step || 0);
            const min = Number(input.min || 0);
            const current = Number(input.value || 0);
            input.value = String(Math.max(min, current + step));
            return;
          }
          const btn = event.target.closest('.invoice-line-remove');
          if (!btn || !form.contains(btn)) {
            return;
          }
          const inputId = btn.getAttribute('aria-controls');
          const input = inputId ? document.getElementById(inputId) : null;
          if (!input) {
            return;
          }
          input.value = '0';
          if (typeof form.requestSubmit === 'function') {
            form.requestSubmit();
          } else {
            form.submit();
          }
        });
      });
    }


    function initCheckoutForms(root) {
      const scope = root || document;
      scope.querySelectorAll('[data-checkout-form]:not([data-checkout-form-init])').forEach((form) => {
        form.setAttribute('data-checkout-form-init', '');
        form.addEventListener('submit', () => {
          form.setAttribute('aria-busy', 'true');
          const submit = document.querySelector('[data-checkout-submit]');
          if (!submit) {
            return;
          }
          submit.disabled = true;
          submit.classList.add('is-loading');
          submit.setAttribute('aria-busy', 'true');
        });
      });
    }

    function initCompaniesSearch(root) {
      const scope = root || document;
      scope.querySelectorAll('#companies-filters:not([data-companies-search-init])').forEach((form) => {
        form.setAttribute('data-companies-search-init', '');
        const input = form.querySelector('input[name="q"]');
        const clearUrl = form.dataset.clearUrl;
        if (!input || !clearUrl) {
          return;
        }
        input.addEventListener('keydown', (event) => {
          if (event.key === 'Enter') {
            event.preventDefault();
            form.requestSubmit();
          }
        });
        input.addEventListener('search', () => {
          if (!input.value.trim()) {
            window.location.href = clearUrl;
          }
        });
      });
    }

    function initPasswordModal(root) {
      const scope = root || document;
      scope.querySelectorAll('#password-change-modal:not([data-password-modal-init])').forEach((dialog) => {
        dialog.setAttribute('data-password-modal-init', '');
        const open = () => {
          if (typeof dialog.showModal === 'function') {
            dialog.showModal();
          }
        };
        const close = () => dialog.close();
        scope.querySelector('[data-password-modal-open]')?.addEventListener('click', open);
        dialog.querySelectorAll('[data-password-modal-close]').forEach((btn) => {
          btn.addEventListener('click', close);
        });
        dialog.addEventListener('click', (event) => {
          if (event.target === dialog) {
            close();
          }
        });
        if (dialog.dataset.autoOpen === '1') {
          open();
        }
      });
    }

    function initCredentialFlash(root) {
      const scope = root || document;
      scope.querySelectorAll('[data-credential-flash]:not([data-credential-flash-init])').forEach((rootEl) => {
        rootEl.setAttribute('data-credential-flash-init', '');
        if (history && history.replaceState) {
          history.replaceState(null, '', '/profile');
        }
        const value = rootEl.dataset.password;
        const masked = rootEl.querySelector('[data-credential-masked]');
        const revealed = rootEl.querySelector('[data-credential-revealed]');
        const showBtn = rootEl.querySelector('[data-credential-show]');
        const hideBtn = rootEl.querySelector('[data-credential-hide]');
        const copyWrap = rootEl.querySelector('[data-credential-copy-wrap]');
        let hideTimer;
        const mask = () => {
          revealed.hidden = true;
          masked.hidden = false;
          revealed.textContent = '';
          if (hideBtn) {
            hideBtn.hidden = true;
          }
        };
        const reveal = () => {
          revealed.textContent = value;
          revealed.hidden = false;
          masked.hidden = true;
          if (hideBtn) {
            hideBtn.hidden = false;
          }
          clearTimeout(hideTimer);
          hideTimer = setTimeout(mask, 30000);
        };
        showBtn?.addEventListener('click', reveal);
        hideBtn?.addEventListener('click', mask);
        copyWrap?.addEventListener('click', async () => {
          try {
            await navigator.clipboard.writeText(value);
          } catch (_err) {
            /* ignore */
          }
        });
      });
    }

    function initInvoicePdfPreview() {
      if (document.body.dataset.invoicePreviewInit === '1') {
        return;
      }
      document.body.dataset.invoicePreviewInit = '1';
      const closePreview = () => {
        const previewDialog = document.getElementById('invoice-pdf-preview-modal');
        const previewFrame = document.getElementById('invoice-pdf-preview-frame');
        if (!previewDialog || !previewFrame) {
          return;
        }
        previewDialog.close();
        previewFrame.removeAttribute('src');
      };
      const openPreview = (url) => {
        if (!url) {
          return;
        }
        const previewDialog = document.getElementById('invoice-pdf-preview-modal');
        const previewFrame = document.getElementById('invoice-pdf-preview-frame');
        if (!previewDialog || !previewFrame) {
          return;
        }
        previewFrame.src = url;
        if (typeof previewDialog.showModal === 'function') {
          previewDialog.showModal();
        }
      };
      document.addEventListener('click', (event) => {
        const closeTrigger = event.target.closest('[data-invoice-preview-close]');
        if (closeTrigger) {
          event.preventDefault();
          closePreview();
          return;
        }
        const trigger = event.target.closest('[data-invoice-preview-open]');
        if (!trigger) {
          return;
        }
        event.preventDefault();
        openPreview(trigger.getAttribute('data-invoice-preview-url'));
      });
      document.addEventListener('click', (event) => {
        const previewDialog = document.getElementById('invoice-pdf-preview-modal');
        if (previewDialog && event.target === previewDialog) {
          closePreview();
        }
      });
    }


    function initAppShell(root) {
      formatLocalDatetimes(root);
      initLucideIcons(root);
      initAlertAutoHide(root);
      initDescriptionToggles(root);
      initProductGalleries(root);
      initFilterBars(root);
      initAuthPages(root);
      initInvoiceItemForms(root);
      initCheckoutForms(root);
      initCompaniesSearch(root);
      initPasswordModal(root);
      initCredentialFlash(root);
    }


    document.body.addEventListener('click', async (event) => {
      const link = event.target.closest('a.stock-reserve-link');
      if (!link) {
        return;
      }
      event.preventDefault();
      const modal = document.getElementById('product-reservations-modal');
      const body = document.getElementById('product-reservations-body');
      const title = document.getElementById('product-reservations-title');
      const url = link.getAttribute('data-reservations-url');
      if (!modal || !body || !url) {
        return;
      }
      if (title) {
        title.textContent = link.getAttribute('data-product-name') || '';
      }
      body.innerHTML = '<p class="text-muted panel-empty">Загрузка…</p>';
      modal.showModal();
      try {
        const response = await fetch(url, { credentials: 'same-origin' });
        if (!response.ok) {
          throw new Error('reserve load failed');
        }
        body.innerHTML = await response.text();
        initAppShell(body);
      } catch (error) {
        body.innerHTML = '<p class="text-muted panel-empty">Не удалось загрузить резервы.</p>';
      }
    });

    function bootAppShell() {
      try {
        initInvoicePdfPreview();
        initAppShell();
        updateSidebarActive();
      } finally {
        document.documentElement.classList.add('icons-ready');
      }
    }

    function htmxSwapRoot(target) {
      if (!(target instanceof Element)) {
        return document;
      }
      if (!target.isConnected && target.id) {
        const live = document.getElementById(target.id);
        if (live) {
          return live;
        }
      }
      return target;
    }

    document.body.addEventListener('htmx:configRequest', (event) => {
      const token = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
      if (token) {
        event.detail.headers['X-CSRF-Token'] = token;
      }
    });

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', bootAppShell);
    } else {
      bootAppShell();
    }

    document.body.addEventListener('htmx:afterSwap', (event) => {
      const target = event.detail.target;
      if (
        target instanceof Element
        && (target.classList.contains('catalog-cart-slot') || target.id === 'layout-cart-count')
      ) {
        if (target.classList.contains('catalog-cart-slot')) {
          initLucideIcons(target);
          initCheckoutForms(target);
        }
        updateSidebarActive();
        return;
      }
      initAppShell(htmxSwapRoot(target));
      updateSidebarActive();
    });
