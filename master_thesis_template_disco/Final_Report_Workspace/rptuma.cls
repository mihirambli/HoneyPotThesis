%===========================================================
% tuklda - an style based on scrartcl usable for
% diploma thesis
%-----------------------------------------------------------
% version: 0.4.0
% licensed under the GPL v2
% written by Thomas Fischer, 03-Apr-2003
% extended by Frank A. Zdarsky, 14-Jan-2005
% extended by Carolina Nogueira, 06-Sep-2018
% extended by Eric Jedermann, 03-Feb-2025
%===========================================================

% Changelog
% 0.0.1:  11-Jul-2002
%   - First Version
% 0.0.2:  15-Jul-2002
%   - table of content, figures, tables added
%   - PDF & PS support (via ifpdf-package) added
%   - bibliography added
%   - index added
% 0.0.3:  17-Jul-2002
%   - ``Eidesstattliche Erkl\"arung'' modified
%   - \sectionquote added
%   - \theindex redefined
% 0.0.4:  02-Sep-2002
%   - ``Abk\"urzungen und Formelzeichen'' (environment glossar) added
% 0.0.5:  17-Sep-2002
%   - \authoremail added
%   - better PDF support (hyperref, thumbnails, ...)
% 0.0.6:  23-Oct-2002
%   - \enlargethispage for title page added, so more space available
% 0.0.7:  18-Dec-2002
%   - flexible vertical spaces on title page (...plus x minus y)
% 0.0.8:  18-Mar-2003
%   - package "ae" for better font support with pdflatex added
% 0.1.0:  03-Apr-2003
%   - field for "Matrikelnummer" added (authorid)
%   - switched from "scrartcl" to "scrbook" as base class
% 0.1.1:  09-Apr-2003
%   - bibliography and tocs via KOMA options in content
%   - scrpage's (KOMA) \headfont and \pnumfont added
% 0.1.2:  25-Aug-2003
%   - replaced \clearpage by \cleardoublepage, used for doubleside printing
% 0.2.0:  15-Jan-2005
%   - renamed from "tuddiplarb" to "tuklda"
%   - using fonts Helvetica and Palatino as default
%   - inserted command \thesisid to number diploma thesises
%   - modified logo to match Kaiserslautern's logo
%   - changed several \cleardoublepage to \clearpage{\pagestyle{empty}
%     \cleardoublepage} for better duplex printing
%   - added command \makecover to create special cover pages containing
%     text only in a small window
%   - modified text for ``Eidesstattliche Erkl\"arung''
%   - title page's layout modified, several spacings changed to \vfill
% 0.3.0: 6-Sep-2018
%	- Adapting class used for Diplom Arbeit into Master Thesis Template
%	- Adding updated TU Kaiserslautern Logo
%	- Changing default language to English
% 0.4.0: 3-Feb-2025
%   - renamed to "rptuma.cls"
%	- Adding RPTU Logo and RPTU address.
%	- Adding option for online/offline thesis submissions:
%		- scrbook option openany: avoid blank pages before a new chapter.
%		- scrbook option oneside: avoid pagewise left-right shifts.
%	- Changing Eidesstattliche Erklärung to Eigenständigkeitserklärung with adaption for including genarative AI.
%
% Documentation hints:
%	- https://www.overleaf.com/learn/latex/Writing_your_own_class
%	- https://stackoverflow.com/questions/52786478

\NeedsTeXFormat{LaTeX2e}

\ProvidesClass{rptuma}[2025/02/03 RPTU Master Thesis]

%% The block below does not work on rhrz-overleaf (works on sci-overleaf and TexMaker), so it is deactivated.
%% It should provide a online/offline option (default=online) when including \documentclass[online]{rptuma}.
%\DeclareOption{online}{\def\rptuma@mode{openany,oneside}}
%\DeclareOption{offline}{\def\rptuma@mode{openright,twoside}}
%\ExecuteOptions{online}
%\ProcessOptions
%\PassOptionsToClass{12pt,a4paper,\rptuma@mode}{scrbook}
%\LoadClass{scrbook}

\LoadClass[12pt,a4paper,openany,oneside]{scrbook}  %(online mode)
%\LoadClass[12pt,a4paper,openright,twoside]{scrbook}  %(offline mode)

\RequirePackage[T1]{fontenc}
\RequirePackage[german,english]{babel}
\RequirePackage{makeidx}
\RequirePackage[nodayofweek]{datetime}

\RequirePackage{ifpdf}
\ifpdf
\RequirePackage[pdftex,colorlinks=true,hypertexnames=false,pdfstartview=FitH,linkcolor=black,citecolor=black,urlcolor=black,pdfborder=0]{hyperref}
\RequirePackage[pdftex]{graphicx}
\else
\RequirePackage[dvips]{graphicx}
\fi

\RequirePackage{mathpazo}
\RequirePackage[scaled]{helvet}

% variables
\newcommand{\TheSubject}{Art der Ausarbeitung}
\renewcommand{\subject}[1]{\AtBeginDocument{\renewcommand{\TheSubject}{#1 \ifpdf\hypersetup{pdfsubject={#1}}\fi}}}
\newcommand{\TheTitle}{Titel der Ausarbeitung}
\renewcommand{\title}[1]{\AtBeginDocument{\renewcommand{\TheTitle}{#1 \ifpdf\hypersetup{pdftitle={#1}}\fi}}}
\newcommand{\TheAuthor}{Name des Erstellers}
\renewcommand{\author}[1]{\AtBeginDocument{\renewcommand{\TheAuthor}{#1 \ifpdf\hypersetup{pdfauthor={#1}}\fi}}}
\newcommand{\TheAuthorEmail}{E-Mail-Adresse}
\newcommand{\authoremail}[1]{\AtBeginDocument{\renewcommand{\TheAuthorEmail}{\ifpdf\href{mailto:#1}{#1}\else#1\fi}}}
\newcommand{\TheAuthorID}{Matrikelnummer}
\newcommand{\authorid}[1]{\AtBeginDocument{\renewcommand{\TheAuthorID}{#1}}}
\newcommand{\TheDate}{Datum}
\newcommand{\TheThesisID}{Nummer der Arbeit}
\newcommand{\thesisid}[1]{\AtBeginDocument{\renewcommand{\TheThesisID}{#1}}}
\renewcommand{\date}[1]{\AtBeginDocument{\renewcommand{\TheDate}{#1}}}
\newcommand{\TheDepartment}{Ort der Ausarbeitung\\(Universit\"at, Fachbereich, Fachgebiet)}
\newcommand{\department}[1]{\AtBeginDocument{\renewcommand{\TheDepartment}{#1}}}
\newcommand{\TheLogo}{\includegraphics[height=2cm]{img/RPTUlogo}}
\newcommand{\logo}[1]{\AtBeginDocument{\renewcommand{\TheLogo}{#1}}}
\newcommand{\TheSupervisor}{Dr. No}
\newcommand{\supervisor}[1]{\AtBeginDocument{\renewcommand{\TheSupervisor}{#1}}}
\newcommand{\TheSupervisorTitle}{Pr\"ufer}
\newcommand{\supervisortitle}[1]{\AtBeginDocument{\renewcommand{\TheSupervisorTitle}{#1}}}
\newcommand{\TheSecondSupervisor}{}
\newcommand{\secondsupervisor}[1]{\AtBeginDocument{\renewcommand{\TheSecondSupervisor}{#1}}}
\newcommand{\TheSecondSupervisorTitle}{}
\newcommand{\secondsupervisortitle}[1]{\AtBeginDocument{\renewcommand{\TheSecondSupervisorTitle}{#1}}}
\newcommand{\ThePlace}{Kaiserslautern}
\newcommand{\place}[1]{\AtBeginDocument{\renewcommand{\ThePlace}{#1}}}
\newcommand{\TheDedication}{}
\renewcommand{\dedication}[1]{\AtBeginDocument{\renewcommand{\TheDedication}{#1}}}
\newcommand{\TheGlossarTitle}{Glossary}
\newcommand{\glossartitle}[1]{\AtBeginDocument{\renewcommand{\TheGlossarTitle}{#1}}}

\newcommand{\sectionquote}[2]{\begin{flushright}\begin{minipage}{.6667\textwidth}\begin{flushright}{\small\itshape #1}\\{\footnotesize #2}\end{flushright}\end{minipage}\end{flushright}}

\newcommand{\dindex}[1]{\index{#1}#1}

\renewcommand{\descriptionlabel}[1]{\hspace{\labelsep} {\bf #1:}}

\renewcommand{\headfont}{\sffamily\itshape\footnotesize}
\renewcommand{\pnumfont}{\sffamily\itshape\footnotesize}
\newfont{\coverpagefonttitle}{phvb8r at 24pt}
\newfont{\coverpagefontname}{phvr8r at 20pt}

\renewcommand{\dateseparator}{.}

\renewenvironment{theindex}{
\clearpage{\pagestyle{empty}\cleardoublepage}
\section*{\indexname}
\addcontentsline{toc}{section}{\indexname}
\makeatletter
\renewcommand{\item}{\@idxitem}
\makeatother
\setlength{\parskip}{0ex}
\setlength{\parindent}{0pt}
\begin{multicols}{3}
}{
\end{multicols}
}


\setlength{\parskip}{1.5ex plus 0.5ex minus 0.5ex}
\setlength{\parindent}{0cm}

\makeindex


\renewcommand{\maketitle}{

\thispagestyle{empty}
\enlargethispage{1cm}

\begin{center}
\TheLogo

\vfill

{\large \TheSubject}

%\vspace{0.8cm minus 0.4cm}
\vfill

\parbox{\textwidth}{\begin{center}\sffamily\bfseries\Huge \TheTitle \end{center}}

%\vspace{0.8cm plus 0.4cm minus 0.3cm}
\vfill

by

\vspace{0.5cm plus 0.5cm minus 0.1cm}

{\large \TheAuthor}

%\TheAuthorEmail

%\TheAuthorID


%\vspace{1.5cm plus 0.5cm minus 0.6cm}
\vfill

%\usdate\displaydate{thesisDate}\\ %(alternative)
\longdate\displaydate{thesisDate}

%\vspace{1.75cm plus 0.3cm minus 0.4cm}
\vfill
\TheDepartment

%\TheThesisID

\vspace{0.75cm minus 0.3cm}


\vspace{1.25cm minus 0.5cm}
\vfill

\begin{tabular}{rl}
\TheSupervisorTitle & \TheSupervisor \\
\TheSecondSupervisorTitle & \TheSecondSupervisor \\
\end{tabular}

\vfill

\setcounter{page}{1}
\thispagestyle{empty}

\end{center}
\clearpage{\pagestyle{empty}\cleardoublepage}
}


\newcommand{\makedeclaration}{
{\bfseries\sffamily Eigenst\"andigkeitserkl\"arung}

%Hiermit versichere ich, die vorliegende Masterarbeit selbstst\"andig verfasst und keine anderen als die angegebenen Quellen und Hilfsmittel verwendet zu haben. Alle w\"ortlich oder sinngem\"a\ss{} \"ubernommenen Zitate sind als solche kenntlich gemacht worden. Diese Arbeit hat in gleicher oder \"ahnlicher Form noch keiner Pr\"ufungsbeh\"orde vorgelegen.

Hier\-mit ver\-si\-che\-re ich, dass ich die von mir vor\-ge\-leg\-te Ar\-beit mit dem The\-ma \glqq \makeatletter\TheTitle\makeatother\grqq{} selb\-stst\"a\-ndig ver\-fasst ha\-be, dass ich die ver\-wen\-de\-ten Quel\-len und Hilfs\-mit\-tel (auch ge\-ne\-ra\-ti\-ve KI-Sys\-te\-me) voll\-st\"an\-dig an\-ge\-ge\-ben ha\-be und dass ich die Stel\-len der Ar\-beit --- ein\-schlie\ss{}\-lich Ta\-bel\-len und Ab\-bil\-dung\-en ---, die an\-de\-ren Wer\-ken oder dem In\-ter\-net im Wort\-laut oder dem Sinn nach ent\-nom\-men sind unter An\-ga\-be der Quel\-le als Ent\-leh\-nung kennt\-lich ge\-macht habe. So\-fern der Ein\-satz ge\-ne\-ra\-ti\-ver KI-Sys\-te\-me \"uber eine rei\-ne Gra\-mma\-tik\-pr\"u\-fung und Stil\-ver\-bess\-er\-ung hi\-naus\-geht, ist der Ein\-satz nach\-voll\-zieh\-bar kennt\-lich ge\-macht. F\"ur Feh\-ler im Text oder Code bin ich ver\-ant\-wort\-lich, auch bei \"Uber\-nahme aus ge\-ne\-ra\-ti\-ven KI-Sys\-te\-men. Die\-se Ar\-beit hat in glei\-cher oder \"ahn\-lich\-er Form noch kei\-ner Pr\"u\-fungs\-be\-h\"or\-de vor\-ge\-le\-gen.


\vspace{1em}

\ThePlace, den \dmyyyydate\displaydate{thesisDate}

\vspace{2cm}
\noindent
\begin{tabular}{p{0.45\textwidth}p{0.45\textwidth}}
\rule{0.4\textwidth}{1pt}\\
\TheAuthor
\end{tabular}

\setcounter{page}{1}
\thispagestyle{empty}
\clearpage{\pagestyle{empty}\cleardoublepage}
}


\newcommand{\makededication}{

\begin{flushright}
\begin{minipage}{.6\textwidth}
\begin{flushright}

\vspace{6cm}

{\sffamily\itshape \TheDedication}
%{\slshape \TheDedication}
\end{flushright}
\end{minipage}
\end{flushright}

\setcounter{page}{1}
\thispagestyle{empty}
\clearpage{\pagestyle{empty}\cleardoublepage}
}


\newcommand{\glossarlabel}[1]{\mbox{#1}\hfil}
\newenvironment{glossar}{
\chapter*{\TheGlossarTitle}\addcontentsline{toc}{chapter}{\TheGlossarTitle}
\begin{list}
{}
{\renewcommand{\makelabel}{\glossarlabel}
 \setlength{\labelwidth}{4cm}
 \setlength{\labelsep}{0.5cm}
 \setlength{\leftmargin}{4.5cm}
}
}
{
\end{list}
\clearpage{\pagestyle{empty}\cleardoublepage}
}
