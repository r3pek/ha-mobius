var H=window,R=H.ShadowRoot&&(H.ShadyCSS===void 0||H.ShadyCSS.nativeShadow)&&"adoptedStyleSheets"in Document.prototype&&"replace"in CSSStyleSheet.prototype,z=Symbol(),st=new WeakMap,x=class{constructor(t,e,i){if(this._$cssResult$=!0,i!==z)throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");this.cssText=t,this.t=e}get styleSheet(){let t=this.o,e=this.t;if(R&&t===void 0){let i=e!==void 0&&e.length===1;i&&(t=st.get(e)),t===void 0&&((this.o=t=new CSSStyleSheet).replaceSync(this.cssText),i&&st.set(e,t))}return t}toString(){return this.cssText}},ot=o=>new x(typeof o=="string"?o:o+"",void 0,z),I=(o,...t)=>{let e=o.length===1?o[0]:t.reduce((i,s,n)=>i+(r=>{if(r._$cssResult$===!0)return r.cssText;if(typeof r=="number")return r;throw Error("Value passed to 'css' function must be a 'css' function result: "+r+". Use 'unsafeCSS' to pass non-literal values, but take care to ensure page security.")})(s)+o[n+1],o[0]);return new x(e,o,z)},D=(o,t)=>{R?o.adoptedStyleSheets=t.map(e=>e instanceof CSSStyleSheet?e:e.styleSheet):t.forEach(e=>{let i=document.createElement("style"),s=H.litNonce;s!==void 0&&i.setAttribute("nonce",s),i.textContent=e.cssText,o.appendChild(i)})},T=R?o=>o:o=>o instanceof CSSStyleSheet?(t=>{let e="";for(let i of t.cssRules)e+=i.cssText;return ot(e)})(o):o;var B,M=window,rt=M.trustedTypes,Et=rt?rt.emptyScript:"",nt=M.reactiveElementPolyfillSupport,W={toAttribute(o,t){switch(t){case Boolean:o=o?Et:null;break;case Object:case Array:o=o==null?o:JSON.stringify(o)}return o},fromAttribute(o,t){let e=o;switch(t){case Boolean:e=o!==null;break;case Number:e=o===null?null:Number(o);break;case Object:case Array:try{e=JSON.parse(o)}catch{e=null}}return e}},lt=(o,t)=>t!==o&&(t==t||o==o),V={attribute:!0,type:String,converter:W,reflect:!1,hasChanged:lt},q="finalized",$=class extends HTMLElement{constructor(){super(),this._$Ei=new Map,this.isUpdatePending=!1,this.hasUpdated=!1,this._$El=null,this._$Eu()}static addInitializer(t){var e;this.finalize(),((e=this.h)!==null&&e!==void 0?e:this.h=[]).push(t)}static get observedAttributes(){this.finalize();let t=[];return this.elementProperties.forEach((e,i)=>{let s=this._$Ep(i,e);s!==void 0&&(this._$Ev.set(s,i),t.push(s))}),t}static createProperty(t,e=V){if(e.state&&(e.attribute=!1),this.finalize(),this.elementProperties.set(t,e),!e.noAccessor&&!this.prototype.hasOwnProperty(t)){let i=typeof t=="symbol"?Symbol():"__"+t,s=this.getPropertyDescriptor(t,i,e);s!==void 0&&Object.defineProperty(this.prototype,t,s)}}static getPropertyDescriptor(t,e,i){return{get(){return this[e]},set(s){let n=this[t];this[e]=s,this.requestUpdate(t,n,i)},configurable:!0,enumerable:!0}}static getPropertyOptions(t){return this.elementProperties.get(t)||V}static finalize(){if(this.hasOwnProperty(q))return!1;this[q]=!0;let t=Object.getPrototypeOf(this);if(t.finalize(),t.h!==void 0&&(this.h=[...t.h]),this.elementProperties=new Map(t.elementProperties),this._$Ev=new Map,this.hasOwnProperty("properties")){let e=this.properties,i=[...Object.getOwnPropertyNames(e),...Object.getOwnPropertySymbols(e)];for(let s of i)this.createProperty(s,e[s])}return this.elementStyles=this.finalizeStyles(this.styles),!0}static finalizeStyles(t){let e=[];if(Array.isArray(t)){let i=new Set(t.flat(1/0).reverse());for(let s of i)e.unshift(T(s))}else t!==void 0&&e.push(T(t));return e}static _$Ep(t,e){let i=e.attribute;return i===!1?void 0:typeof i=="string"?i:typeof t=="string"?t.toLowerCase():void 0}_$Eu(){var t;this._$E_=new Promise(e=>this.enableUpdating=e),this._$AL=new Map,this._$Eg(),this.requestUpdate(),(t=this.constructor.h)===null||t===void 0||t.forEach(e=>e(this))}addController(t){var e,i;((e=this._$ES)!==null&&e!==void 0?e:this._$ES=[]).push(t),this.renderRoot!==void 0&&this.isConnected&&((i=t.hostConnected)===null||i===void 0||i.call(t))}removeController(t){var e;(e=this._$ES)===null||e===void 0||e.splice(this._$ES.indexOf(t)>>>0,1)}_$Eg(){this.constructor.elementProperties.forEach((t,e)=>{this.hasOwnProperty(e)&&(this._$Ei.set(e,this[e]),delete this[e])})}createRenderRoot(){var t;let e=(t=this.shadowRoot)!==null&&t!==void 0?t:this.attachShadow(this.constructor.shadowRootOptions);return D(e,this.constructor.elementStyles),e}connectedCallback(){var t;this.renderRoot===void 0&&(this.renderRoot=this.createRenderRoot()),this.enableUpdating(!0),(t=this._$ES)===null||t===void 0||t.forEach(e=>{var i;return(i=e.hostConnected)===null||i===void 0?void 0:i.call(e)})}enableUpdating(t){}disconnectedCallback(){var t;(t=this._$ES)===null||t===void 0||t.forEach(e=>{var i;return(i=e.hostDisconnected)===null||i===void 0?void 0:i.call(e)})}attributeChangedCallback(t,e,i){this._$AK(t,i)}_$EO(t,e,i=V){var s;let n=this.constructor._$Ep(t,i);if(n!==void 0&&i.reflect===!0){let r=(((s=i.converter)===null||s===void 0?void 0:s.toAttribute)!==void 0?i.converter:W).toAttribute(e,i.type);this._$El=t,r==null?this.removeAttribute(n):this.setAttribute(n,r),this._$El=null}}_$AK(t,e){var i;let s=this.constructor,n=s._$Ev.get(t);if(n!==void 0&&this._$El!==n){let r=s.getPropertyOptions(n),h=typeof r.converter=="function"?{fromAttribute:r.converter}:((i=r.converter)===null||i===void 0?void 0:i.fromAttribute)!==void 0?r.converter:W;this._$El=n,this[n]=h.fromAttribute(e,r.type),this._$El=null}}requestUpdate(t,e,i){let s=!0;t!==void 0&&(((i=i||this.constructor.getPropertyOptions(t)).hasChanged||lt)(this[t],e)?(this._$AL.has(t)||this._$AL.set(t,e),i.reflect===!0&&this._$El!==t&&(this._$EC===void 0&&(this._$EC=new Map),this._$EC.set(t,i))):s=!1),!this.isUpdatePending&&s&&(this._$E_=this._$Ej())}async _$Ej(){this.isUpdatePending=!0;try{await this._$E_}catch(e){Promise.reject(e)}let t=this.scheduleUpdate();return t!=null&&await t,!this.isUpdatePending}scheduleUpdate(){return this.performUpdate()}performUpdate(){var t;if(!this.isUpdatePending)return;this.hasUpdated,this._$Ei&&(this._$Ei.forEach((s,n)=>this[n]=s),this._$Ei=void 0);let e=!1,i=this._$AL;try{e=this.shouldUpdate(i),e?(this.willUpdate(i),(t=this._$ES)===null||t===void 0||t.forEach(s=>{var n;return(n=s.hostUpdate)===null||n===void 0?void 0:n.call(s)}),this.update(i)):this._$Ek()}catch(s){throw e=!1,this._$Ek(),s}e&&this._$AE(i)}willUpdate(t){}_$AE(t){var e;(e=this._$ES)===null||e===void 0||e.forEach(i=>{var s;return(s=i.hostUpdated)===null||s===void 0?void 0:s.call(i)}),this.hasUpdated||(this.hasUpdated=!0,this.firstUpdated(t)),this.updated(t)}_$Ek(){this._$AL=new Map,this.isUpdatePending=!1}get updateComplete(){return this.getUpdateComplete()}getUpdateComplete(){return this._$E_}shouldUpdate(t){return!0}update(t){this._$EC!==void 0&&(this._$EC.forEach((e,i)=>this._$EO(i,this[i],e)),this._$EC=void 0),this._$Ek()}updated(t){}firstUpdated(t){}};$[q]=!0,$.elementProperties=new Map,$.elementStyles=[],$.shadowRootOptions={mode:"open"},nt?.({ReactiveElement:$}),((B=M.reactiveElementVersions)!==null&&B!==void 0?B:M.reactiveElementVersions=[]).push("1.6.3");var K,L=window,S=L.trustedTypes,at=S?S.createPolicy("lit-html",{createHTML:o=>o}):void 0,J="$lit$",_=`lit$${(Math.random()+"").slice(9)}$`,$t="?"+_,wt=`<${$t}>`,A=document,O=()=>A.createComment(""),U=o=>o===null||typeof o!="object"&&typeof o!="function",_t=Array.isArray,xt=o=>_t(o)||typeof o?.[Symbol.iterator]=="function",F=`[ 	
\f\r]`,C=/<(?:(!--|\/[^a-zA-Z])|(\/?[a-zA-Z][^>\s]*)|(\/?$))/g,ht=/-->/g,ct=/>/g,g=RegExp(`>|${F}(?:([^\\s"'>=/]+)(${F}*=${F}*(?:[^ 	
\f\r"'\`<>=]|("|')|))|$)`,"g"),dt=/'/g,ut=/"/g,mt=/^(?:script|style|textarea|title)$/i,ft=o=>(t,...e)=>({_$litType$:o,strings:t,values:e}),m=ft(1),Mt=ft(2),b=Symbol.for("lit-noChange"),c=Symbol.for("lit-nothing"),pt=new WeakMap,y=A.createTreeWalker(A,129,null,!1);function gt(o,t){if(!Array.isArray(o)||!o.hasOwnProperty("raw"))throw Error("invalid template strings array");return at!==void 0?at.createHTML(t):t}var Ct=(o,t)=>{let e=o.length-1,i=[],s,n=t===2?"<svg>":"",r=C;for(let h=0;h<e;h++){let l=o[h],a,d,u=-1,p=0;for(;p<l.length&&(r.lastIndex=p,d=r.exec(l),d!==null);)p=r.lastIndex,r===C?d[1]==="!--"?r=ht:d[1]!==void 0?r=ct:d[2]!==void 0?(mt.test(d[2])&&(s=RegExp("</"+d[2],"g")),r=g):d[3]!==void 0&&(r=g):r===g?d[0]===">"?(r=s??C,u=-1):d[1]===void 0?u=-2:(u=r.lastIndex-d[2].length,a=d[1],r=d[3]===void 0?g:d[3]==='"'?ut:dt):r===ut||r===dt?r=g:r===ht||r===ct?r=C:(r=g,s=void 0);let v=r===g&&o[h+1].startsWith("/>")?" ":"";n+=r===C?l+wt:u>=0?(i.push(a),l.slice(0,u)+J+l.slice(u)+_+v):l+_+(u===-2?(i.push(void 0),h):v)}return[gt(o,n+(o[e]||"<?>")+(t===2?"</svg>":"")),i]},P=class o{constructor({strings:t,_$litType$:e},i){let s;this.parts=[];let n=0,r=0,h=t.length-1,l=this.parts,[a,d]=Ct(t,e);if(this.el=o.createElement(a,i),y.currentNode=this.el.content,e===2){let u=this.el.content,p=u.firstChild;p.remove(),u.append(...p.childNodes)}for(;(s=y.nextNode())!==null&&l.length<h;){if(s.nodeType===1){if(s.hasAttributes()){let u=[];for(let p of s.getAttributeNames())if(p.endsWith(J)||p.startsWith(_)){let v=d[r++];if(u.push(p),v!==void 0){let St=s.getAttribute(v.toLowerCase()+J).split(_),k=/([.?@])?(.*)/.exec(v);l.push({type:1,index:n,name:k[2],strings:St,ctor:k[1]==="."?Z:k[1]==="?"?Q:k[1]==="@"?X:w})}else l.push({type:6,index:n})}for(let p of u)s.removeAttribute(p)}if(mt.test(s.tagName)){let u=s.textContent.split(_),p=u.length-1;if(p>0){s.textContent=S?S.emptyScript:"";for(let v=0;v<p;v++)s.append(u[v],O()),y.nextNode(),l.push({type:2,index:++n});s.append(u[p],O())}}}else if(s.nodeType===8)if(s.data===$t)l.push({type:2,index:n});else{let u=-1;for(;(u=s.data.indexOf(_,u+1))!==-1;)l.push({type:7,index:n}),u+=_.length-1}n++}}static createElement(t,e){let i=A.createElement("template");return i.innerHTML=t,i}};function E(o,t,e=o,i){var s,n,r,h;if(t===b)return t;let l=i!==void 0?(s=e._$Co)===null||s===void 0?void 0:s[i]:e._$Cl,a=U(t)?void 0:t._$litDirective$;return l?.constructor!==a&&((n=l?._$AO)===null||n===void 0||n.call(l,!1),a===void 0?l=void 0:(l=new a(o),l._$AT(o,e,i)),i!==void 0?((r=(h=e)._$Co)!==null&&r!==void 0?r:h._$Co=[])[i]=l:e._$Cl=l),l!==void 0&&(t=E(o,l._$AS(o,t.values),l,i)),t}var G=class{constructor(t,e){this._$AV=[],this._$AN=void 0,this._$AD=t,this._$AM=e}get parentNode(){return this._$AM.parentNode}get _$AU(){return this._$AM._$AU}u(t){var e;let{el:{content:i},parts:s}=this._$AD,n=((e=t?.creationScope)!==null&&e!==void 0?e:A).importNode(i,!0);y.currentNode=n;let r=y.nextNode(),h=0,l=0,a=s[0];for(;a!==void 0;){if(h===a.index){let d;a.type===2?d=new N(r,r.nextSibling,this,t):a.type===1?d=new a.ctor(r,a.name,a.strings,this,t):a.type===6&&(d=new Y(r,this,t)),this._$AV.push(d),a=s[++l]}h!==a?.index&&(r=y.nextNode(),h++)}return y.currentNode=A,n}v(t){let e=0;for(let i of this._$AV)i!==void 0&&(i.strings!==void 0?(i._$AI(t,i,e),e+=i.strings.length-2):i._$AI(t[e])),e++}},N=class o{constructor(t,e,i,s){var n;this.type=2,this._$AH=c,this._$AN=void 0,this._$AA=t,this._$AB=e,this._$AM=i,this.options=s,this._$Cp=(n=s?.isConnected)===null||n===void 0||n}get _$AU(){var t,e;return(e=(t=this._$AM)===null||t===void 0?void 0:t._$AU)!==null&&e!==void 0?e:this._$Cp}get parentNode(){let t=this._$AA.parentNode,e=this._$AM;return e!==void 0&&t?.nodeType===11&&(t=e.parentNode),t}get startNode(){return this._$AA}get endNode(){return this._$AB}_$AI(t,e=this){t=E(this,t,e),U(t)?t===c||t==null||t===""?(this._$AH!==c&&this._$AR(),this._$AH=c):t!==this._$AH&&t!==b&&this._(t):t._$litType$!==void 0?this.g(t):t.nodeType!==void 0?this.$(t):xt(t)?this.T(t):this._(t)}k(t){return this._$AA.parentNode.insertBefore(t,this._$AB)}$(t){this._$AH!==t&&(this._$AR(),this._$AH=this.k(t))}_(t){this._$AH!==c&&U(this._$AH)?this._$AA.nextSibling.data=t:this.$(A.createTextNode(t)),this._$AH=t}g(t){var e;let{values:i,_$litType$:s}=t,n=typeof s=="number"?this._$AC(t):(s.el===void 0&&(s.el=P.createElement(gt(s.h,s.h[0]),this.options)),s);if(((e=this._$AH)===null||e===void 0?void 0:e._$AD)===n)this._$AH.v(i);else{let r=new G(n,this),h=r.u(this.options);r.v(i),this.$(h),this._$AH=r}}_$AC(t){let e=pt.get(t.strings);return e===void 0&&pt.set(t.strings,e=new P(t)),e}T(t){_t(this._$AH)||(this._$AH=[],this._$AR());let e=this._$AH,i,s=0;for(let n of t)s===e.length?e.push(i=new o(this.k(O()),this.k(O()),this,this.options)):i=e[s],i._$AI(n),s++;s<e.length&&(this._$AR(i&&i._$AB.nextSibling,s),e.length=s)}_$AR(t=this._$AA.nextSibling,e){var i;for((i=this._$AP)===null||i===void 0||i.call(this,!1,!0,e);t&&t!==this._$AB;){let s=t.nextSibling;t.remove(),t=s}}setConnected(t){var e;this._$AM===void 0&&(this._$Cp=t,(e=this._$AP)===null||e===void 0||e.call(this,t))}},w=class{constructor(t,e,i,s,n){this.type=1,this._$AH=c,this._$AN=void 0,this.element=t,this.name=e,this._$AM=s,this.options=n,i.length>2||i[0]!==""||i[1]!==""?(this._$AH=Array(i.length-1).fill(new String),this.strings=i):this._$AH=c}get tagName(){return this.element.tagName}get _$AU(){return this._$AM._$AU}_$AI(t,e=this,i,s){let n=this.strings,r=!1;if(n===void 0)t=E(this,t,e,0),r=!U(t)||t!==this._$AH&&t!==b,r&&(this._$AH=t);else{let h=t,l,a;for(t=n[0],l=0;l<n.length-1;l++)a=E(this,h[i+l],e,l),a===b&&(a=this._$AH[l]),r||(r=!U(a)||a!==this._$AH[l]),a===c?t=c:t!==c&&(t+=(a??"")+n[l+1]),this._$AH[l]=a}r&&!s&&this.j(t)}j(t){t===c?this.element.removeAttribute(this.name):this.element.setAttribute(this.name,t??"")}},Z=class extends w{constructor(){super(...arguments),this.type=3}j(t){this.element[this.name]=t===c?void 0:t}},Ot=S?S.emptyScript:"",Q=class extends w{constructor(){super(...arguments),this.type=4}j(t){t&&t!==c?this.element.setAttribute(this.name,Ot):this.element.removeAttribute(this.name)}},X=class extends w{constructor(t,e,i,s,n){super(t,e,i,s,n),this.type=5}_$AI(t,e=this){var i;if((t=(i=E(this,t,e,0))!==null&&i!==void 0?i:c)===b)return;let s=this._$AH,n=t===c&&s!==c||t.capture!==s.capture||t.once!==s.once||t.passive!==s.passive,r=t!==c&&(s===c||n);n&&this.element.removeEventListener(this.name,this,s),r&&this.element.addEventListener(this.name,this,t),this._$AH=t}handleEvent(t){var e,i;typeof this._$AH=="function"?this._$AH.call((i=(e=this.options)===null||e===void 0?void 0:e.host)!==null&&i!==void 0?i:this.element,t):this._$AH.handleEvent(t)}},Y=class{constructor(t,e,i){this.element=t,this.type=6,this._$AN=void 0,this._$AM=e,this.options=i}get _$AU(){return this._$AM._$AU}_$AI(t){E(this,t)}};var vt=L.litHtmlPolyfillSupport;vt?.(P,N),((K=L.litHtmlVersions)!==null&&K!==void 0?K:L.litHtmlVersions=[]).push("2.8.0");var yt=(o,t,e)=>{var i,s;let n=(i=e?.renderBefore)!==null&&i!==void 0?i:t,r=n._$litPart$;if(r===void 0){let h=(s=e?.renderBefore)!==null&&s!==void 0?s:null;n._$litPart$=r=new N(t.insertBefore(O(),h),h,void 0,e??{})}return r._$AI(o),r};var tt,et;var f=class extends ${constructor(){super(...arguments),this.renderOptions={host:this},this._$Do=void 0}createRenderRoot(){var t,e;let i=super.createRenderRoot();return(t=(e=this.renderOptions).renderBefore)!==null&&t!==void 0||(e.renderBefore=i.firstChild),i}update(t){let e=this.render();this.hasUpdated||(this.renderOptions.isConnected=this.isConnected),super.update(t),this._$Do=yt(e,this.renderRoot,this.renderOptions)}connectedCallback(){var t;super.connectedCallback(),(t=this._$Do)===null||t===void 0||t.setConnected(!0)}disconnectedCallback(){var t;super.disconnectedCallback(),(t=this._$Do)===null||t===void 0||t.setConnected(!1)}render(){return b}};f.finalized=!0,f._$litElement$=!0,(tt=globalThis.litElementHydrateSupport)===null||tt===void 0||tt.call(globalThis,{LitElement:f});var At=globalThis.litElementPolyfillSupport;At?.({LitElement:f});((et=globalThis.litElementVersions)!==null&&et!==void 0?et:globalThis.litElementVersions=[]).push("3.3.3");var j="None",Ut={sunrise:"mdi:weather-sunset-up",sunset:"mdi:weather-sunset-down",feed:"mdi:food-drumstick",feeding:"mdi:food-drumstick",storm:"mdi:weather-lightning-rainy",moon:"mdi:weather-night",moonlit:"mdi:weather-night",clean:"mdi:broom","water change":"mdi:water-sync"},Pt="mdi:bookmark-outline";function bt(o){let t=o.toLowerCase();for(let[e,i]of Object.entries(Ut))if(t.includes(e))return i;return Pt}function Nt(o){let t=Math.floor(o/60),e=o%60;return`${t}:${String(e).padStart(2,"0")}`}var it=class extends f{static get properties(){return{hass:{attribute:!1},_config:{state:!0}}}setConfig(t){if(!t||!t.entity)throw new Error('mobius-scene-card: "entity" is required (a select.*_scene_selection entity)');this._config=t}getCardSize(){return 3}static getStubConfig(t,e){return{entity:(e||[]).find(s=>s.startsWith("select.")&&s.includes("scene"))||""}}_stateObj(){return this.hass&&this._config?this.hass.states[this._config.entity]:void 0}_selectOption(t){let e=this._stateObj();e&&this.hass.callService("select","select_option",{entity_id:e.entity_id,option:t})}_renderTile(t,e){let i=t===j?"mdi:calendar-clock":bt(t),s=t===j?"Normal schedule":t;return m`
      <button
        class="tile ${e?"active":""}"
        @click=${()=>this._selectOption(t)}
        title=${s}
      >
        ${e?m`<ha-icon class="check" icon="mdi:check-circle"></ha-icon>`:c}
        <ha-icon icon=${i}></ha-icon>
        <span>${s}</span>
      </button>
    `}render(){if(!this._config)return c;let t=this._stateObj();if(!t)return m`
        <ha-card>
          <div class="warning">
            Entity not found: <code>${this._config.entity}</code>
          </div>
        </ha-card>
      `;let e=t.state,i=t.attributes.options||[j],s=t.attributes.duration_remaining_seconds,n=e!==j?e:null;return m`
      <ha-card>
        <div class="header">
          <div class="title">${t.attributes.friendly_name||"Scene"}</div>
        </div>
        ${n?m`
              <div class="status active">
                <ha-icon icon=${bt(n)}></ha-icon>
                <div class="status-text">
                  <div class="status-main">${n} active</div>
                  ${s!=null?m`<div class="status-sub">${Nt(s)} remaining</div>`:c}
                </div>
              </div>
            `:m`<div class="status">Running the normal schedule.</div>`}
        <div class="tiles">
          ${i.map(r=>this._renderTile(r,r===e))}
        </div>
      </ha-card>
    `}static get styles(){return I`
      ha-card {
        padding: 16px;
      }
      .header {
        margin-bottom: 12px;
      }
      .title {
        font-size: 1.2em;
        font-weight: 500;
        color: var(--primary-text-color);
      }
      .status {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 10px 12px;
        border-radius: 10px;
        margin-bottom: 14px;
        font-size: 0.9em;
        color: var(--secondary-text-color);
      }
      .status.active {
        background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.08);
        color: var(--primary-text-color);
      }
      .status.active ha-icon {
        color: var(--primary-color);
      }
      .status-main {
        font-weight: 500;
      }
      .status-sub {
        font-size: 0.85em;
        color: var(--secondary-text-color);
      }
      .warning {
        padding: 12px;
        color: var(--error-color, #db4437);
      }
      .tiles {
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
      }
      .tile {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 6px;
        width: 84px;
        height: 84px;
        border-radius: 12px;
        border: 1px solid var(--divider-color);
        background: var(--card-background-color);
        color: var(--primary-text-color);
        cursor: pointer;
        font-family: inherit;
        font-size: 0.75em;
        padding: 0 6px;
        position: relative;
        box-sizing: border-box;
      }
      .tile:hover {
        border-color: var(--primary-color);
      }
      .tile.active {
        border-color: var(--primary-color);
        border-width: 1.5px;
        background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.08);
        color: var(--primary-color);
        font-weight: 500;
      }
      .tile ha-icon {
        --mdc-icon-size: 22px;
      }
      .tile .check {
        position: absolute;
        top: 4px;
        right: 4px;
        --mdc-icon-size: 16px;
        color: var(--primary-color);
        background: var(--card-background-color);
        border-radius: 50%;
      }
      .tile span {
        text-align: center;
        line-height: 1.15;
        overflow: hidden;
        text-overflow: ellipsis;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
      }
    `}};customElements.define("mobius-scene-card",it);window.customCards=window.customCards||[];window.customCards.push({type:"mobius-scene-card",name:"Mobius Scene",description:"Activate a Mobius tank's scenes, or resume its normal schedule.",preview:!1});
/*! Bundled license information:

@lit/reactive-element/css-tag.js:
  (**
   * @license
   * Copyright 2019 Google LLC
   * SPDX-License-Identifier: BSD-3-Clause
   *)

@lit/reactive-element/reactive-element.js:
  (**
   * @license
   * Copyright 2017 Google LLC
   * SPDX-License-Identifier: BSD-3-Clause
   *)

lit-html/lit-html.js:
  (**
   * @license
   * Copyright 2017 Google LLC
   * SPDX-License-Identifier: BSD-3-Clause
   *)

lit-element/lit-element.js:
  (**
   * @license
   * Copyright 2017 Google LLC
   * SPDX-License-Identifier: BSD-3-Clause
   *)

lit-html/is-server.js:
  (**
   * @license
   * Copyright 2022 Google LLC
   * SPDX-License-Identifier: BSD-3-Clause
   *)
*/
